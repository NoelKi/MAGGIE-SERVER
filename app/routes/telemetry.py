from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt

from app.services.influx_service import write_telemetry, write_telemetry_batch, query_telemetry, ping_influx

telemetry_bp = Blueprint("telemetry", __name__)


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/telemetry  — Datenpunkt schreiben
# ──────────────────────────────────────────────────────────────────────────────

@telemetry_bp.route("/telemetry", methods=["POST"])
@jwt_required()
def ingest():
    """
    Schreibt einen Telemetrie-Datenpunkt in InfluxDB.
    Nur Nutzer mit Rolle 'admin' oder 'operator' erlaubt.

    Body (JSON):
    {
        "measurement": "sensors",        // Pflicht – Name der Messung
        "fields": {                       // Pflicht – Messwerte (min. 1)
            "temperature": 23.4,
            "pressure":    1013.25
        },
        "tags": {                         // Optional – Metadaten
            "sensor_id": "imu_1"
        }
    }
    """
    claims = get_jwt()
    if claims.get("role") not in ("admin", "operator"):
        return jsonify({"error": "Forbidden"}), 403

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    measurement = body.get("measurement", "").strip()
    fields      = body.get("fields")
    tags        = body.get("tags")

    if not measurement:
        return jsonify({"error": "'measurement' is required"}), 400
    if not fields or not isinstance(fields, dict):
        return jsonify({"error": "'fields' must be a non-empty dict"}), 400

    try:
        write_telemetry(measurement=measurement, fields=fields, tags=tags or {})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "InfluxDB write failed", "detail": str(exc)}), 502

    return jsonify({"status": "ok", "measurement": measurement}), 201


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/telemetry  — Datenpunkte abfragen
# ──────────────────────────────────────────────────────────────────────────────

@telemetry_bp.route("/telemetry", methods=["GET"])
@jwt_required()
def fetch():
    """
    Liest Telemetrie-Datenpunkte aus InfluxDB.

    Query-Parameter:
        measurement  (str,  Pflicht)  – z.B. "sensors"
        start        (str,  optional) – Flux-Zeit, Standard "-1h"
        stop         (str,  optional) – Flux-Zeit, Standard "now()"
        limit        (int,  optional) – Max. Datenpunkte, Standard 500

    Beispiel:
        GET /api/telemetry?measurement=sensors&start=-30m&limit=100
    """
    measurement = request.args.get("measurement", "").strip()
    if not measurement:
        return jsonify({"error": "'measurement' query param is required"}), 400

    start = request.args.get("start", "-1h")
    stop  = request.args.get("stop",  "now()")

    try:
        limit = int(request.args.get("limit", 500))
    except ValueError:
        return jsonify({"error": "'limit' must be an integer"}), 400

    try:
        data = query_telemetry(measurement=measurement, start=start, stop=stop, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "InfluxDB query failed", "detail": str(exc)}), 502

    return jsonify({"measurement": measurement, "count": len(data), "data": data}), 200


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/telemetry/ping  — InfluxDB erreichbar?
# ──────────────────────────────────────────────────────────────────────────────

@telemetry_bp.route("/telemetry/ping", methods=["GET"])
@jwt_required()
def influx_ping():
    """
    GET /api/telemetry/ping
    Prüft ob InfluxDB erreichbar ist.
    Returns: { "influx": true/false }
    """
    return jsonify({"influx": ping_influx()}), 200


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/telemetry/batch  — Mehrere Datenpunkte auf einmal schreiben
# ──────────────────────────────────────────────────────────────────────────────

@telemetry_bp.route("/telemetry/batch", methods=["POST"])
@jwt_required()
def ingest_batch():
    """
    Schreibt mehrere Telemetrie-Datenpunkte in einem einzigen Request.
    Ideal für Mikrocontroller die mehrere Sensoren gleichzeitig auslesen.

    Body (JSON):
    {
        "points": [
            {
                "measurement": "imu",
                "fields": { "ax": 0.12, "ay": -0.03, "az": 9.81 },
                "tags":   { "sensor_id": "imu_1", "phase": "ascent" }
            },
            {
                "measurement": "environment",
                "fields": { "temperature": 21.3, "pressure": 1012.1 },
                "tags":   { "sensor_id": "env_1" }
            }
        ]
    }
    """
    claims = get_jwt()
    if claims.get("role") not in ("admin", "operator"):
        return jsonify({"error": "Forbidden"}), 403

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    points = body.get("points")
    if not points or not isinstance(points, list):
        return jsonify({"error": "'points' must be a non-empty list"}), 400

    # Jeden Punkt validieren
    for i, p in enumerate(points):
        if not p.get("measurement", "").strip():
            return jsonify({"error": f"points[{i}]: 'measurement' is required"}), 400
        if not p.get("fields") or not isinstance(p["fields"], dict):
            return jsonify({"error": f"points[{i}]: 'fields' must be a non-empty dict"}), 400

    try:
        written = write_telemetry_batch(points)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "InfluxDB batch write failed", "detail": str(exc)}), 502

    return jsonify({"status": "ok", "written": written}), 201

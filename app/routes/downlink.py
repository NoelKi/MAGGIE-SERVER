"""
MAGGIE – RXSM Downlink Routes
=============================

REST-Schnittstelle für den seriellen RXSM-Downlink (Live-Rohbytes).

  GET  /api/downlink/status           — Verbindungsstatus + Statistik
  GET  /api/downlink/raw?since=<n>    — neue Rohbytes ab Cursor <n> (Hex)
  GET  /api/downlink/ports            — verfügbare serielle Ports
  POST /api/downlink/connect          — Reader auf Port/Baud (neu)starten
  POST /api/downlink/disconnect       — Reader stoppen
  POST /api/downlink/clear            — Puffer leeren

Der /raw-Endpoint ist zum Pollen gedacht: der Client merkt sich den
zurückgegebenen `cursor` und schickt ihn als `since` beim nächsten Aufruf.
"""

from flask import Blueprint, request, jsonify

from app.services.serial_listener import downlink

downlink_bp = Blueprint("downlink", __name__)


@downlink_bp.route("/downlink/status", methods=["GET"])
def status():
    """Verbindungsstatus + Byte- und Decode-Statistik des seriellen Downlinks."""
    snap = downlink.buffer.snapshot()
    snap["running"] = downlink.is_running()
    snap["decode"] = downlink.decode_snapshot()
    return jsonify(snap), 200


@downlink_bp.route("/downlink/frames", methods=["GET"])
def frames():
    """
    Liefert decodierte OBC-Downlink-Frames ab Cursor `since`.

    Query:
        since  (int, optional)  — letzter Frame-Cursor des Clients (default 0)

    Response:
        {
          "cursor":       <neuer Cursor — beim nächsten Aufruf als since senden>,
          "frames":       [ { counter, time_ms, subsystem, type, fields, ... }, ... ],
          "frames_total": <insgesamt decodiert>,
          "crc_errors":   <Frames mit CRC-Fehler>
        }
    """
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        return jsonify({"error": "'since' must be an integer"}), 400

    frame_list, cursor = downlink.decoded.since(since)
    dec = downlink.decode_snapshot()

    return jsonify({
        "cursor":       cursor,
        "frames":       frame_list,
        "frames_total": dec["frames_total"],
        "crc_errors":   dec["crc_errors"],
    }), 200


@downlink_bp.route("/downlink/raw", methods=["GET"])
def raw():
    """
    Liefert neue Rohbytes ab dem Cursor `since`.

    Query:
        since  (int, optional)  — letzter Cursor des Clients (default 0)

    Response:
        {
          "cursor":        <neuer Cursor — beim nächsten Aufruf als since senden>,
          "hex":           "eb90a5...",   // neue Bytes als Hex-String
          "count":         <Anzahl neuer Bytes>,
          "dropped":       <Bytes, die seit `since` aus dem Puffer fielen>,
          "total_bytes":   <insgesamt empfangen>,
          "bytes_per_sec": <aktuelle Rate>,
          "connected":     true/false
        }
    """
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        return jsonify({"error": "'since' must be an integer"}), 400

    data, cursor, dropped = downlink.buffer.read_since(since)
    snap = downlink.buffer.snapshot()

    return jsonify({
        "cursor":        cursor,
        "hex":           data.hex(),
        "count":         len(data),
        "dropped":       dropped,
        "total_bytes":   snap["total_bytes"],
        "bytes_per_sec": snap["bytes_per_sec"],
        "connected":     snap["connected"],
    }), 200


@downlink_bp.route("/downlink/ports", methods=["GET"])
def ports():
    """Listet verfügbare serielle Ports (für die Port-Auswahl in der GUI)."""
    return jsonify({
        "available": downlink.buffer.snapshot()["available"],
        "ports":     downlink.list_ports(),
    }), 200


@downlink_bp.route("/downlink/connect", methods=["POST"])
def connect():
    """
    (Neu-)Startet den seriellen Reader.

    Body (JSON): { "port": "/dev/tty.usbserial-XXXX", "baud": 38400 }
    """
    body = request.get_json(silent=True) or {}
    port = (body.get("port") or "").strip()
    if not port:
        return jsonify({"error": "'port' is required"}), 400

    try:
        baud = int(body.get("baud", 38400))
    except (TypeError, ValueError):
        return jsonify({"error": "'baud' must be an integer"}), 400

    try:
        downlink.start(port, baud)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    return jsonify({"status": "ok", "port": port, "baud": baud}), 200


@downlink_bp.route("/downlink/disconnect", methods=["POST"])
def disconnect():
    """Stoppt den seriellen Reader."""
    downlink.stop()
    return jsonify({"status": "ok"}), 200


@downlink_bp.route("/downlink/clear", methods=["POST"])
def clear():
    """Leert den Byte-Puffer (Cursor läuft weiter)."""
    downlink.buffer.clear()
    return jsonify({"status": "ok"}), 200

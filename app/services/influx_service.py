from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import current_app
from influxdb_client.client.influxdb_client import InfluxDBClient
from influxdb_client.client.write.point import Point
from influxdb_client.domain.write_precision import WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _client() -> InfluxDBClient:
    """Erstellt einen InfluxDBClient aus der aktuellen Flask-Config."""
    cfg = current_app.config
    return InfluxDBClient(
        url=cfg["INFLUX_URL"],
        token=cfg["INFLUX_TOKEN"],
        org=cfg["INFLUX_ORG"],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Write
# ──────────────────────────────────────────────────────────────────────────────

def write_telemetry(measurement: str, fields: dict[str, Any], tags: dict[str, str] | None = None) -> None:
    """
    Schreibt einen einzelnen Telemetrie-Datenpunkt in InfluxDB.

    Args:
        measurement: Name der Messung, z.B. "sensors" oder "gps"
        fields:      Dict mit Messwerten, z.B. {"temperature": 23.4, "pressure": 1013.25}
        tags:        Optionale Metadaten, z.B. {"sensor_id": "imu_1", "unit": "celsius"}
    """
    point = Point(measurement)

    if tags:
        for key, value in tags.items():
            point = point.tag(key, value)

    for key, value in fields.items():
        point = point.field(key, value)

    point = point.time(datetime.now(timezone.utc), WritePrecision.MS)

    with _client() as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(
            bucket=current_app.config["INFLUX_BUCKET"],
            org=current_app.config["INFLUX_ORG"],
            record=point,
        )


def write_telemetry_batch(points: list[dict[str, Any]]) -> int:
    """
    Schreibt mehrere Telemetrie-Datenpunkte in einem einzigen InfluxDB-Write.

    Args:
        points: Liste von Dicts, jedes mit:
                - measurement (str, Pflicht)
                - fields      (dict, Pflicht)
                - tags        (dict, optional)

    Returns:
        Anzahl erfolgreich geschriebener Punkte
    """
    now = datetime.now(timezone.utc)
    records: list[Point] = []

    for p in points:
        point = Point(p["measurement"])

        for key, value in (p.get("tags") or {}).items():
            point = point.tag(key, value)

        for key, value in p["fields"].items():
            point = point.field(key, value)

        point = point.time(now, WritePrecision.MS)
        records.append(point)

    with _client() as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(
            bucket=current_app.config["INFLUX_BUCKET"],
            org=current_app.config["INFLUX_ORG"],
            record=records,
        )

    return len(records)


# ──────────────────────────────────────────────────────────────────────────────
# Query
# ──────────────────────────────────────────────────────────────────────────────

def query_telemetry(
    measurement: str,
    start: str = "-1h",
    stop: str = "now()",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Liest Telemetrie-Datenpunkte aus InfluxDB.

    Args:
        measurement: Name der Messung, z.B. "sensors"
        start:       Flux-Zeitangabe, z.B. "-1h", "-30m", "2026-03-07T00:00:00Z"
        stop:        Flux-Zeitangabe, Standard "now()"
        limit:       Maximale Anzahl Datenpunkte

    Returns:
        Liste von Dicts: [{"time": ..., "field": ..., "value": ..., "tags": {...}}, ...]
    """
    bucket = current_app.config["INFLUX_BUCKET"]
    org    = current_app.config["INFLUX_ORG"]

    flux = f"""
        from(bucket: "{bucket}")
          |> range(start: {start}, stop: {stop})
          |> filter(fn: (r) => r["_measurement"] == "{measurement}")
          |> limit(n: {limit})
    """

    results: list[dict[str, Any]] = []

    with _client() as client:
        query_api = client.query_api()
        tables = query_api.query(flux, org=org)

        for table in tables:
            for record in table.records:
                results.append({
                    "time":        record.get_time().isoformat(),
                    "measurement": record.get_measurement(),
                    "field":       record.get_field(),
                    "value":       record.get_value(),
                    "tags": {
                        k: v for k, v in record.values.items()
                        if k not in ("_start", "_stop", "_time", "_value", "_field", "_measurement", "result", "table")
                    },
                })

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────────────────────────────────────

def ping_influx() -> bool:
    """Gibt True zurück, wenn InfluxDB erreichbar ist."""
    try:
        with _client() as client:
            return client.ping()
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("InfluxDB ping failed: %s", exc)
        return False

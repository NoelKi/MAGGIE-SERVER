"""
MAGGIE – Stream Service (Socket.IO Hub)
========================================

Zentraler Broadcast-Hub für Echtzeit-Telemetrie über Socket.IO.

Der ``packet_listener`` ruft ``emit_telemetry()`` auf, sobald ein
neues OBC-Paket geparst wurde.  Alle verbundenen Clients erhalten
das Event sofort.

Socket.IO-Events (Server → Client):
─────────────────────────────────────────────────
  telemetry:imu            IMU-Sensordaten (ax, ay, az, gx, gy, gz)
  telemetry:environment    Temp / Druck / Feuchte
  telemetry:gps            Lat / Lon / Alt / Speed
  telemetry:system         CPU-Temp / Batterie / Uptime
  telemetry:arm            Arm-Status (Joints, Strom, Fehler)
  telemetry:hrdm           HRDM-Status (deployed, locked)
  obc:heartbeat            OBC-Heartbeat (seq, boot_count)
  obc:state                OBC-Snapshot (Mission-State, Flags …)
  obc:flags                Flag-Update (liftoff, sods, soex …)
  command:ack              Bestätigung für gesendeten Befehl

Socket.IO-Events (Client → Server):
─────────────────────────────────────────────────
  subscribe                Client meldet sich an (optional: Räume)
  request:state            Client fordert aktuellen OBC-Snapshot an
"""

from __future__ import annotations

import logging
from typing import Any

from flask_socketio import SocketIO, emit

log = logging.getLogger(__name__)

# ── Singleton Socket.IO-Instanz ──────────────────────────────────────────────
# Wird in extensions.py erstellt und in der App Factory initialisiert.

socketio: SocketIO | None = None


def init_socketio(sio: SocketIO) -> None:
    """Setzt die globale Socket.IO-Referenz (einmalig aus create_app)."""
    global socketio  # noqa: PLW0603
    socketio = sio
    _register_handlers()
    log.info("Stream-Service initialisiert (Socket.IO)")


def _register_handlers() -> None:
    """Registriert Socket.IO-Event-Handler (Client → Server)."""
    if socketio is None:
        return

    @socketio.on("connect")
    def handle_connect() -> None:
        log.info("Client verbunden")
        # Sofort aktuellen OBC-State senden
        from app.services.obc_state_service import obc_state
        emit("obc:state", obc_state.snapshot())

    @socketio.on("disconnect")
    def handle_disconnect() -> None:
        log.info("Client getrennt")

    @socketio.on("request:state")
    def handle_request_state() -> None:
        """Client fordert aktuellen OBC-Zustand an."""
        from app.services.obc_state_service import obc_state
        emit("obc:state", obc_state.snapshot())


# ── Emit-Funktionen (vom packet_listener aufgerufen) ─────────────────────────

def emit_telemetry(measurement: str, fields: dict[str, Any], header: dict[str, Any] | None = None) -> None:
    """
    Broadcastet ein Telemetrie-Event an alle verbundenen Clients.

    Args:
        measurement:  "imu", "environment", "gps", "system", "arm", "hrdm"
        fields:       Geparste Felder aus dem OBC-Paket
        header:       Optionale Header-Infos (seq, timestamp_ms, flags, phase)
    """
    if socketio is None:
        return

    event = f"telemetry:{measurement}"
    payload: dict[str, Any] = {**fields}
    if header:
        payload["_seq"] = header.get("sequence", 0)
        payload["_ts"]  = header.get("timestamp_ms", 0)
        payload["_phase"] = header.get("flight_phase", "")

    socketio.emit(event, payload)


def emit_heartbeat(seq: int, boot_count: int) -> None:
    """Broadcastet ein OBC-Heartbeat-Event."""
    if socketio is None:
        return
    socketio.emit("obc:heartbeat", {"seq": seq, "boot_count": boot_count})


def emit_obc_state() -> None:
    """Broadcastet den kompletten OBC-Snapshot."""
    if socketio is None:
        return
    from app.services.obc_state_service import obc_state
    socketio.emit("obc:state", obc_state.snapshot())


def emit_flags(flags: dict[str, bool]) -> None:
    """Broadcastet aktualisierte RXSM-Flags."""
    if socketio is None:
        return
    socketio.emit("obc:flags", flags)


def emit_command_ack(cmd_id: int, success: bool, detail: str = "") -> None:
    """Broadcastet eine Bestätigung für einen gesendeten Befehl."""
    if socketio is None:
        return
    socketio.emit("command:ack", {
        "cmd_id": cmd_id,
        "success": success,
        "detail": detail,
    })

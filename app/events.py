"""
MAGGIE – Socket.IO Event-Handler
================================

Gegenstück zu MAGGIE-GS/src/app/core/services/obc.service.ts.

Server → Client (Broadcasts, siehe app/services/packet_listener.py):
  obc:state              Vollständiger ObcSnapshot
  obc:heartbeat          { seq, boot_count }
  obc:flags              FlagState (RXSM-Signale)
  command:ack            { cmd_id, success, detail }
  telemetry:imu          ImuData          (+ _seq, _ts, _phase)
  telemetry:environment  EnvironmentData  (+ _seq, _ts, _phase)
  telemetry:system       SystemData       (+ _seq, _ts, _phase)

Client → Server:
  request:state          Fordert einen sofortigen obc:state an

Der Modul-Import in create_app() registriert die Handler — deshalb steht
hier kein Blueprint, sondern nur die Dekoratoren.
"""

import logging

from flask import request
from flask_socketio import emit

from app.extensions import socketio
from app.services.obc_state import obc_state

log = logging.getLogger(__name__)


@socketio.on("connect")
def on_connect() -> None:
    """Neuer GS-Client — sofort den aktuellen Zustand nachliefern."""
    log.info("Socket.IO: Client verbunden (sid=%s)", request.sid)
    emit("obc:state", obc_state.snapshot())
    emit("obc:flags", obc_state.flags())


@socketio.on("disconnect")
def on_disconnect(reason: str | None = None) -> None:
    """Client getrennt — reason wird erst ab Flask-SocketIO 5.5 übergeben."""
    log.info("Socket.IO: Client getrennt (sid=%s, reason=%s)", request.sid, reason)


@socketio.on("request:state")
def on_request_state() -> None:
    """Der Client fragt den Zustand aktiv ab (ObcService.requestState())."""
    emit("obc:state", obc_state.snapshot())

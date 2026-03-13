"""
MAGGIE – UDP Packet Listener
=============================

Empfängt binäre OBC-Pakete über UDP, schreibt sie in InfluxDB
und broadcastet sie über Socket.IO an alle verbundenen Clients.

Läuft als Daemon-Thread parallel zum Flask-Server.

Konfiguration (via .env / config/settings.py):
  UDP_HOST      Bind-Adresse   (default: "0.0.0.0")
  UDP_PORT      Port           (default: 9000)
  UDP_TIMEOUT   Socket-Timeout (default: 2.0 s)

Verwendung:
  from app.services.packet_listener import start_udp_listener
  start_udp_listener(app)   # einmalig in create_app() aufrufen
"""

import socket
import logging
import threading
from flask import Flask

from app.services.packet_parser import (
    parse_packet,
    TYPE_HEARTBEAT,
    TYPE_ARM_STATUS,
    TYPE_HRDM_STATUS,
    TYPE_LIGHT_STATUS,
    TYPE_MISSION_STATE,
    TYPE_CMD_ACK,
    TYPE_SYSTEM,
    PACKET_SIZE,
)
from app.services.influx_service import write_telemetry
from app.services.obc_state_service import obc_state
from app.services import stream_service

log = logging.getLogger(__name__)

# Singleton-Referenz — verhindert doppelten Thread-Start
_listener_lock   = threading.Lock()
_listener_thread: threading.Thread | None = None


# ── Haupt-Loop ────────────────────────────────────────────────────────────────
def _udp_loop(app: Flask) -> None:
    """Blockierender UDP-Empfangs-Loop — läuft im Daemon-Thread."""
    host = app.config.get("UDP_HOST", "0.0.0.0")
    port = int(app.config.get("UDP_PORT", 9000))
    timeout = float(app.config.get("UDP_TIMEOUT", 2.0))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.bind((host, port))
        except OSError as exc:
            log.error("UDP-Listener: Bind auf %s:%d fehlgeschlagen: %s", host, port, exc)
            return

        log.info("UDP-Listener gestartet auf %s:%d (max. %d Bytes/Paket)",
                 host, port, PACKET_SIZE)

        while True:
            try:
                raw, addr = sock.recvfrom(256)   # 256 > 64 Bytes Paketgröße
            except socket.timeout:
                continue   # Kein Paket — normales Timeout, Loop weiter
            except OSError:
                log.warning("UDP-Listener: Socket-Fehler — beende Loop")
                break

            _handle_packet(app, raw, addr)


def _handle_packet(app: Flask, raw: bytes, addr: tuple) -> None:
    """Parst ein Paket, aktualisiert OBC-State, broadcastet via Socket.IO und schreibt in InfluxDB."""
    pkt = parse_packet(raw)

    if not pkt.valid:
        log.warning("Ungültiges Paket von %s:%d — %s", addr[0], addr[1], pkt.error)
        return

    hdr = pkt.header
    measurement = hdr.type_name
    phase       = hdr.flight_phase

    log.debug(
        "Paket #%d | Type=%s | Phase=%s | ts=%dms | von %s:%d",
        hdr.sequence, measurement, phase, hdr.timestamp_ms, addr[0], addr[1],
    )

    # ── 1) OBC State aktualisieren ───────────────────────────────────────────
    obc_state.update_header(
        seq=hdr.sequence,
        timestamp_ms=hdr.timestamp_ms,
        flags=hdr.flags,
        flight_phase=phase,
    )

    # ── 2) Typ-spezifische State-Updates ─────────────────────────────────────

    if hdr.pkt_type == TYPE_HEARTBEAT:
        obc_state.update_heartbeat(hdr.sequence, pkt.fields.get("boot_count", -1))
        stream_service.emit_heartbeat(hdr.sequence, pkt.fields.get("boot_count", -1))
        stream_service.emit_obc_state()
        log.info(
            "HEARTBEAT von %s:%d | seq=%d boot_count=%d",
            addr[0], addr[1], hdr.sequence, pkt.fields.get("boot_count", -1),
        )
        return   # Heartbeat nicht in InfluxDB speichern

    if hdr.pkt_type == TYPE_ARM_STATUS:
        f = pkt.fields
        obc_state.update_arm(
            j1_pos=f.get("joint1_position", 0.0),
            j2_pos=f.get("joint2_position", 0.0),
            j1_cur=f.get("joint1_current", 0.0),
            j2_cur=f.get("joint2_current", 0.0),
            error_code=f.get("error_code", 0),
            status_flags=f.get("status_flags", 0),
        )

    elif hdr.pkt_type == TYPE_HRDM_STATUS:
        obc_state.update_hrdm(
            deployed=pkt.fields.get("deployed", False),
            locked=pkt.fields.get("locked", True),
        )

    elif hdr.pkt_type == TYPE_LIGHT_STATUS:
        obc_state.update_light(pkt.fields.get("on", False))

    elif hdr.pkt_type == TYPE_MISSION_STATE:
        obc_state.update_mission_state(pkt.fields.get("mission_state", "STARTUP"))
        obc_state.update_operation_mode(pkt.fields.get("operation_mode", "NONE"))
        stream_service.emit_obc_state()

    elif hdr.pkt_type == TYPE_CMD_ACK:
        stream_service.emit_command_ack(
            cmd_id=pkt.fields.get("cmd_id", 0),
            success=pkt.fields.get("success", False),
            detail=f"detail_code={pkt.fields.get('detail_code', 0)}",
        )
        return   # Command-ACK nicht in InfluxDB speichern

    elif hdr.pkt_type == TYPE_SYSTEM:
        obc_state.update_system(pkt.fields.get("uptime_s", 0.0) * 1000)

    # ── 3) Socket.IO Broadcast ───────────────────────────────────────────────
    header_info = {
        "sequence":     hdr.sequence,
        "timestamp_ms": hdr.timestamp_ms,
        "flight_phase": phase,
    }
    stream_service.emit_telemetry(measurement, pkt.fields, header_info)

    # ── 4) InfluxDB schreiben ────────────────────────────────────────────────
    tags = {
        "phase":    phase,
        "source":   addr[0],
        "seq":      str(hdr.sequence),
    }

    fields = dict(pkt.fields)
    fields["timestamp_ms"] = hdr.timestamp_ms
    fields["flags"]        = hdr.flags

    with app.app_context():
        try:
            write_telemetry(measurement, fields, tags)
        except (OSError, RuntimeError, ValueError) as exc:
            log.error(
                "InfluxDB-Schreibfehler für Paket #%d: %s", hdr.sequence, exc
            )


# ── Öffentliche API ───────────────────────────────────────────────────────────
def start_udp_listener(app: Flask) -> None:
    """
    Startet den UDP-Listener als Daemon-Thread.

    Sicher mehrfach aufrufbar — zweiter Aufruf wird ignoriert.
    """
    with _listener_lock:
        # pylint: disable=global-statement
        global _listener_thread  # noqa: PLW0603

        if _listener_thread is not None and _listener_thread.is_alive():
            log.debug("UDP-Listener läuft bereits — kein zweiter Start")
            return

        _listener_thread = threading.Thread(
            target=_udp_loop,
            args=(app,),
            name="udp-obc-listener",
            daemon=True,   # Thread stirbt mit dem Hauptprozess
        )
        _listener_thread.start()
    log.info("UDP-OBC-Listener-Thread gestartet (daemon=True)")

"""
MAGGIE – UDP Packet Listener
=============================

Empfängt binäre OBC-Pakete über UDP, broadcastet sie live per Socket.IO an
die Ground Station und schreibt sie in InfluxDB.

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

from app.extensions import socketio
from app.services.packet_parser import (
    parse_packet,
    TYPE_HEARTBEAT,
    TYPE_SYSTEM,
    PACKET_SIZE,
)
from app.services.influx_service import write_telemetry
from app.services.obc_state import obc_state, emit_state, emit_flags

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
    """Parst ein Paket, broadcastet es per Socket.IO und schreibt es in InfluxDB."""
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

    # State-Store füttern — meldet, ob Flags/Phase/Online sich geändert haben
    state_changed = obc_state.note_packet(hdr)

    # Heartbeat nur loggen und broadcasten, nicht speichern
    if hdr.pkt_type == TYPE_HEARTBEAT:
        boot_count = pkt.fields.get("boot_count", -1)
        log.info(
            "HEARTBEAT von %s:%d | seq=%d boot_count=%d",
            addr[0], addr[1], hdr.sequence, boot_count,
        )
        obc_state.note_heartbeat(boot_count)
        socketio.emit("obc:heartbeat", {
            "seq":        hdr.sequence,
            "boot_count": boot_count,
        })
        _emit_state_if_changed(state_changed)
        return

    # Uptime aus SYSTEM-Paketen in den Snapshot übernehmen
    if hdr.pkt_type == TYPE_SYSTEM and "uptime_s" in pkt.fields:
        obc_state.note_uptime(pkt.fields["uptime_s"])

    # ── Live-Broadcast an die Ground Station ────────────────────────────
    # Vor dem InfluxDB-Write: eine langsame DB darf die GUI nicht ausbremsen.
    # Die Meta-Felder entsprechen dem GS-Interface TelemetryMeta.
    socketio.emit(f"telemetry:{measurement}", {
        **pkt.fields,
        "_seq":   hdr.sequence,
        "_ts":    hdr.timestamp_ms,
        "_phase": phase,
    })
    _emit_state_if_changed(state_changed)

    # Tags: Flugphase, Quelle-IP, Sequenznummer für Lücken-Erkennung
    tags = {
        "phase":    phase,
        "source":   addr[0],
        "seq":      str(hdr.sequence),
    }

    # Extra-Feld: Zeitstempel seit Lift-off in ms
    fields = dict(pkt.fields)
    fields["timestamp_ms"] = hdr.timestamp_ms
    fields["flags"]        = hdr.flags

    # In InfluxDB schreiben — mit Flask-App-Kontext
    with app.app_context():
        try:
            write_telemetry(measurement, fields, tags)
        except (OSError, RuntimeError, ValueError) as exc:
            log.error(
                "InfluxDB-Schreibfehler für Paket #%d: %s", hdr.sequence, exc
            )


def _emit_state_if_changed(changed: bool) -> None:
    """
    Broadcastet Flags und Snapshot nur bei echter Änderung.

    Bei voller Datenrate wäre ein obc:state pro Paket reine Bandbreiten-
    verschwendung — Flags und Flugphase ändern sich nur selten.
    """
    if not changed:
        return
    emit_flags()
    emit_state()


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

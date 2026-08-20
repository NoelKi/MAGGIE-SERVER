"""
MAGGIE – RXSM Downlink Serial Listener
======================================

Liest den RXSM-Downlink als seriellen Byte-Stream (RS-232/RS-422/USB) und
hält die zuletzt empfangenen Rohbytes in einem thread-sicheren Ringpuffer.

Ziel dieser ersten Stufe:  die eingehenden Bytes UNVERÄNDERT sichtbar machen
(Live-Hexdump in der GUI), damit die Frame-Struktur des Downlinks verifiziert
werden kann, BEVOR ein Decoder gebaut wird.

Architektur:
  Serieller Port ──[Reader-Thread]──► DownlinkBuffer (Ringpuffer)
                                            │
                          REST /api/downlink/raw?since=<cursor>
                                            ▼
                                    GUI-Hexdump-Ansicht

Der Reader-Thread ist zur Laufzeit steuerbar (start/stop mit Port + Baud),
sodass der Port direkt in der GUI gewählt werden kann, ohne den Server
neu zu starten.

Konfiguration (config/settings.py / .env):
  SERIAL_PORT          Portname (leer → zur Laufzeit wählen)
  SERIAL_BAUD          Baudrate (RXSM-Standard: 38400)
  SERIAL_AUTOSTART     beim Start automatisch verbinden (wenn Port gesetzt)
  SERIAL_BUFFER_BYTES  Größe des Ringpuffers in Bytes
"""

from __future__ import annotations

import time
import logging
import threading
from collections import deque
from typing import Optional

from app.services.downlink_frame_parser import (
    FrameExtractor,
    DecodedStore,
    DownlinkFrame,
)
from app.services.obc_state import obc_state, emit_state

log = logging.getLogger(__name__)

# pyserial ist optional — fehlt es, bleibt der Rest des Servers funktionsfähig.
try:
    import serial
    from serial.tools import list_ports
    SERIAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    serial = None            # type: ignore[assignment]
    list_ports = None        # type: ignore[assignment]
    SERIAL_AVAILABLE = False
    log.warning("pyserial nicht installiert — RXSM-Downlink deaktiviert "
                "(pip install pyserial)")


# ── Ringpuffer ────────────────────────────────────────────────────────────────
class DownlinkBuffer:
    """
    Thread-sicherer Byte-Ringpuffer mit monotonem Cursor.

    Der Cursor (`total`) zählt ALLE je empfangenen Bytes. Clients merken sich
    ihren letzten Cursor und fragen mit `read_since(cursor)` nur die neuen Bytes
    ab. Ältere Bytes fallen bei Überlauf hinten raus → `dropped` signalisiert
    das dem Client.
    """

    def __init__(self, capacity: int) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._capacity = max(capacity, 4096)
        self._total = 0             # Cursor: Gesamtzahl empfangener Bytes
        self._start = 0             # Cursor-Wert des ersten Bytes im Puffer

        # Gleitendes 1-s-Fenster für die Byte-Rate
        self._rx: deque[tuple[float, int]] = deque()

        # Vom Reader-Thread gesetzter Verbindungsstatus
        self.connected = False
        self.port: Optional[str] = None
        self.baud: Optional[int] = None
        self.error: Optional[str] = None
        self.last_rx: float = 0.0

    # -- Schreiben (Reader-Thread) ------------------------------------------
    def append(self, data: bytes) -> None:
        if not data:
            return
        now = time.time()
        with self._lock:
            self._buf.extend(data)
            self._total += len(data)
            overflow = len(self._buf) - self._capacity
            if overflow > 0:
                del self._buf[:overflow]
                self._start += overflow
            self.last_rx = now
            self._rx.append((now, len(data)))

    def set_status(self, connected: bool, port: Optional[str],
                   baud: Optional[int], error: Optional[str]) -> None:
        with self._lock:
            self.connected = connected
            self.port = port
            self.baud = baud
            self.error = error

    def clear(self) -> None:
        """Verwirft gepufferte Bytes (Cursor läuft weiter)."""
        with self._lock:
            self._start += len(self._buf)
            self._buf.clear()

    # -- Lesen (REST-Thread) ------------------------------------------------
    def read_since(self, since: int) -> tuple[bytes, int, int]:
        """
        Gibt (neue_bytes, neuer_cursor, dropped) ab Cursor `since` zurück.

        dropped > 0  → seit `since` sind Bytes aus dem Puffer gefallen
                       (Client sollte eine Lücke anzeigen).
        """
        with self._lock:
            if since < 0 or since > self._total:
                # Unbekannter/zurückgesetzter Cursor → ab jetzt neu einsteigen
                return b"", self._total, 0
            if since < self._start:
                dropped = self._start - since
                return bytes(self._buf), self._total, dropped
            idx = since - self._start
            return bytes(self._buf[idx:]), self._total, 0

    def rate_bps(self) -> int:
        """Byte/s über das letzte 1-Sekunden-Fenster."""
        now = time.time()
        with self._lock:
            while self._rx and now - self._rx[0][0] > 1.0:
                self._rx.popleft()
            return sum(count for _, count in self._rx)

    def snapshot(self) -> dict:
        """Status-Snapshot für /api/downlink/status."""
        return {
            "available":     SERIAL_AVAILABLE,
            "connected":     self.connected,
            "port":          self.port,
            "baud":          self.baud,
            "error":         self.error,
            "total_bytes":   self._total,
            "bytes_per_sec": self.rate_bps(),
            "last_rx":       self.last_rx or None,
        }


# ── Serieller Reader mit Laufzeit-Steuerung ────────────────────────────────────
class SerialDownlink:
    """Steuert den seriellen Reader-Thread (start/stop zur Laufzeit)."""

    INFLUX_COOLDOWN_S = 5.0    # Pause nach fehlgeschlagenem InfluxDB-Write
    FLUSH_S           = 0.2    # InfluxDB-Batch-Intervall (5 Writes/s statt 40)
    MAX_PENDING       = 5000   # Sicherheitskappe für den Write-Puffer

    def __init__(self, capacity: int = 256 * 1024) -> None:
        self.buffer = DownlinkBuffer(capacity)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # ── Frame-Decoding ────────────────────────────────────────────
        self.app = None                     # Flask-App für InfluxDB-Writes
        self.extractor = FrameExtractor()   # Byte-Strom → 20-Byte-Frames
        self.decoded = DecodedStore()       # decodierte Frames für die GUI
        self.decode_enabled = True          # OBC-Frames automatisch decodieren
        self._influx_cooldown_until = 0.0   # Pause nach InfluxDB-Fehler

        # ── Gebündelte InfluxDB-Writes ────────────────────────────────
        self._pending: list[dict] = []               # gesammelte Punkte
        self._pending_lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._flush_stop = threading.Event()

    # -- Öffentliche API ----------------------------------------------------
    def start(self, port: str, baud: int) -> None:
        """(Neu-)Startet den Reader für den angegebenen Port + Baudrate."""
        if not SERIAL_AVAILABLE:
            raise RuntimeError("pyserial nicht installiert")
        with self._lock:
            self._stop_locked()
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._loop,
                args=(port, int(baud), self._stop),
                name="serial-downlink",
                daemon=True,
            )
            self._thread.start()
        log.info("RXSM-Downlink-Reader gestartet: %s @ %d Baud", port, baud)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
        self.buffer.set_status(False, self.buffer.port, self.buffer.baud, None)
        log.info("RXSM-Downlink-Reader gestoppt")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @staticmethod
    def list_ports() -> list[dict]:
        """Verfügbare serielle Ports auflisten (für die GUI-Auswahl)."""
        if not SERIAL_AVAILABLE:
            return []
        ports = []
        for p in list_ports.comports():
            ports.append({
                "device":       p.device,
                "description":  p.description or "",
                "manufacturer": p.manufacturer or "",
            })
        return ports

    # -- Intern -------------------------------------------------------------
    def _stop_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=2.0)
        self._thread = None

    def _loop(self, port: str, baud: int, stop: threading.Event) -> None:
        """Reader-Loop mit Auto-Reconnect — läuft im Daemon-Thread."""
        while not stop.is_set():
            try:
                ser = serial.Serial(port, baud, timeout=0.2)
            except (OSError, serial.SerialException) as exc:
                self.buffer.set_status(False, port, baud, str(exc))
                log.warning("Serieller Port %s nicht offen: %s — Retry in 2 s",
                            port, exc)
                stop.wait(2.0)
                continue

            self.buffer.set_status(True, port, baud, None)
            log.info("Seriell verbunden: %s @ %d Baud", port, baud)
            try:
                while not stop.is_set():
                    waiting = ser.in_waiting
                    data = ser.read(waiting or 1)   # blockt max. timeout
                    if data:
                        self.buffer.append(data)     # Rohbytes (Hexdump)
                        if self.decode_enabled:
                            for frame in self.extractor.feed(data):
                                self._on_frame(frame)
            except (OSError, serial.SerialException) as exc:
                self.buffer.set_status(False, port, baud, str(exc))
                log.warning("Serieller Lesefehler auf %s: %s", port, exc)
            finally:
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass

            if not stop.is_set():
                stop.wait(1.0)   # kurz warten, dann Reconnect-Versuch

    # -- Frame-Verarbeitung -------------------------------------------------
    def _on_frame(self, frame: DownlinkFrame) -> None:
        """Ein decodiertes OBC-Frame: für die GUI merken + zum Batch-Write puffern."""
        self.decoded.add(frame)

        # Jedes Frame hält den OBC online; SYS/STATE liefert den Missionszustand.
        state_name = frame.fields.get("state_name") if frame.subsystem_name == "sys" else None
        if obc_state.note_downlink(state_name):
            emit_state()

        measurement = frame.measurement
        if not (measurement and frame.fields and self.app):
            return

        # Punkt für den nächsten Batch puffern (NICHT sofort schreiben).
        # seq als FELD (nicht Tag) → vermeidet unbegrenzte Serien-Kardinalität.
        point = {
            "measurement": measurement,
            "tags":        {"source": "downlink", "type": frame.type_name},
            "fields":      {**frame.fields, "seq": frame.counter, "obc_time_ms": frame.time_ms},
            "time_ms":     int(time.time() * 1000),   # echter Per-Punkt-Zeitstempel
        }
        with self._pending_lock:
            self._pending.append(point)
            if len(self._pending) > self.MAX_PENDING:
                del self._pending[:len(self._pending) - self.MAX_PENDING]

    def ensure_flush_thread(self) -> None:
        """Startet den Batch-Flush-Daemon (einmalig)."""
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return
        self._flush_stop.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="downlink-influx-flush", daemon=True,
        )
        self._flush_thread.start()

    def _flush_loop(self) -> None:
        """Schreibt die gepufferten Punkte periodisch als Batch nach InfluxDB."""
        from app.services.influx_service import write_telemetry_batch
        while not self._flush_stop.wait(self.FLUSH_S):
            with self._pending_lock:
                if not self._pending:
                    continue
                batch = self._pending
                self._pending = []

            # Bei InfluxDB-Störung: Batch verwerfen (Live-Ansicht bleibt unberührt).
            if not self.app or time.time() < self._influx_cooldown_until:
                continue
            try:
                with self.app.app_context():
                    write_telemetry_batch(batch)
            except Exception as exc:  # noqa: BLE001 — Flush-Thread niemals crashen lassen
                self._influx_cooldown_until = time.time() + self.INFLUX_COOLDOWN_S
                log.warning("InfluxDB-Batch-Write (%d Punkte) fehlgeschlagen (%ds Pause): %s",
                            len(batch), self.INFLUX_COOLDOWN_S, exc)

    def decode_snapshot(self) -> dict:
        """Decode-Statistik für /api/downlink/status."""
        snap = self.decoded.snapshot()
        snap["crc_errors"]   = self.extractor.crc_errors
        snap["resync_bytes"] = self.extractor.resync_bytes
        return snap


# Modul-Singleton — geteilt zwischen Reader-Thread und REST-Routes.
downlink = SerialDownlink()


def init_serial_listener(app) -> None:
    """
    Initialisiert den Downlink aus der App-Config und startet ggf. automatisch.

    Einmalig in create_app() aufrufen.
    """
    downlink.buffer._capacity = max(
        int(app.config.get("SERIAL_BUFFER_BYTES", 256 * 1024)), 4096
    )
    downlink.app = app   # für InfluxDB-Writes decodierter Frames
    downlink.ensure_flush_thread()   # gebündelte Writes (5/s statt 40/s)

    if not SERIAL_AVAILABLE:
        return

    port = (app.config.get("SERIAL_PORT") or "").strip()
    baud = int(app.config.get("SERIAL_BAUD", 38400))
    autostart = bool(app.config.get("SERIAL_AUTOSTART", True))

    if port and autostart:
        try:
            downlink.start(port, baud)
        except RuntimeError as exc:
            log.warning("Serieller Autostart fehlgeschlagen: %s", exc)
    else:
        log.info("Serieller Downlink im Standby (kein Port konfiguriert — "
                 "Port zur Laufzeit über /api/downlink/connect wählen)")

"""
MAGGIE – RXSM Telecommand Uplink (serieller TX)
================================================

Sendet Telecommands an das RXSM (im Test das REXUS Test Module, im Experiment
das REXUS Service Module) über einen SEPARATEN seriellen Port. Das RXSM leitet
die SDC-Nutzbytes über die UART an den OBC weiter.

Bewusst getrennt vom read-only Downlink-Reader (serial_listener.py):
  - andere Richtung (TX statt RX), eigener Lebenszyklus
  - eigener serial.Serial-Handle + Lock für thread-sicheres Schreiben

TC_SERIAL_PORT und SERIAL_PORT dürfen derselbe Adapter sein (im Bodenaufbau
führt ein FTDI beide Richtungen von OBC Serial8) — dann liegen zwei Handles
auf demselben Device-Node, was unter macOS/Linux zulässig ist. Wichtig dabei:
wird der Adapter neu enumeriert, verliert der TX-Handle seinen Node still
(macOS: "[Errno 6] Device not configured" beim Schreiben). send_sdc() öffnet
den Port in dem Fall selbst neu — analog zum Reconnect-Loop des Readers.

Frame-Encoder: rxsm_tc_parser.build_sdc_packet() (24-Byte RXSM-TC, SDC=0xA5).

Konfiguration (config/settings.py / .env):
  TC_SERIAL_PORT       Portname zum RXSM-(Test-)Modul (leer → zur Laufzeit wählen)
  TC_SERIAL_BAUD       Baudrate (RXSM-Standard: 38400)
  TC_AUTOSTART         beim Start automatisch verbinden (wenn Port gesetzt)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:  # pyserial optional
    serial = None
    SERIAL_AVAILABLE = False

from app.services.rxsm_tc_parser import build_sdc_packet

log = logging.getLogger(__name__)


class TcUplink:
    """Serieller Telecommand-Sender zum RXSM (start/stop zur Laufzeit)."""

    def __init__(self) -> None:
        self._ser: Optional["serial.Serial"] = None
        self._lock = threading.Lock()
        self._mcnt = 0
        self._node: Optional[tuple] = None   # (st_rdev, st_ino) beim Öffnen
        self._wanted = False                 # bewusst verbunden (nicht per stop() getrennt)
        self.port = ""
        self.baud = 38400
        self.error: Optional[str] = None

    # -- Öffentliche API ----------------------------------------------------
    def start(self, port: str, baud: int) -> None:
        """(Neu-)Öffnet den seriellen TC-Port."""
        if not SERIAL_AVAILABLE:
            raise RuntimeError("pyserial nicht installiert")
        with self._lock:
            self.port = port
            self.baud = int(baud)
            self._open_locked()
            self._wanted = True
        log.info("RXSM-TC-Uplink verbunden: %s @ %d Baud", port, baud)

    def stop(self) -> None:
        with self._lock:
            self._wanted = False
            self._close_locked()
        log.info("RXSM-TC-Uplink getrennt")

    def is_running(self) -> bool:
        return self._ser is not None and self._ser.is_open and self._node_alive()

    def send_sdc(self, dest: int, payload: bytes) -> dict:
        """
        Verpackt payload als SDC-Telecommand und schreibt es auf den TC-Port.

        Args:
            dest:    UART-Ziel-Adresse am RXSM (0-7).
            payload: Nutzdaten (max. 15 Bytes) — hier das Motor-Command-Frame.

        Returns:
            { "mcnt": <int>, "dest": <int>, "sent_hex": <str>, "bytes": <int> }

        Raises:
            RuntimeError: wenn der TC-Port nicht offen ist oder der Write scheitert.
        """
        with self._lock:
            if not (self._wanted and self.port):
                raise RuntimeError("TC-Uplink nicht verbunden")

            mcnt = self._mcnt
            self._mcnt = (self._mcnt + 1) & 0xFF
            packet = build_sdc_packet(mcnt=mcnt, dest=dest, payload=payload)

            try:
                self._write_locked(packet)
            except (OSError, serial.SerialException) as exc:
                # Ein toter Handle ist der Normalfall, wenn der USB-Adapter
                # zwischendurch neu enumeriert wurde (macOS: [Errno 6] Device
                # not configured). Der alte fd zeigt dann auf einen Device-Node,
                # den es nicht mehr gibt. Einmal neu öffnen und wiederholen —
                # der Downlink-Reader macht dasselbe über seinen Reconnect-Loop.
                log.warning("TC-Write fehlgeschlagen (%s) — Port %s wird neu geöffnet",
                            exc, self.port)
                try:
                    self._open_locked()
                    self._write_locked(packet)
                except (OSError, serial.SerialException, RuntimeError) as retry_exc:
                    self.error = str(retry_exc)
                    raise RuntimeError(
                        f"TC-Write fehlgeschlagen: {retry_exc}") from retry_exc
                log.info("TC-Uplink nach Reconnect wieder gesendet (mcnt %d)", mcnt)

            self.error = None

        return {
            "mcnt":     mcnt,
            "dest":     dest,
            "sent_hex": packet.hex(),
            "bytes":    len(packet),
        }

    def snapshot(self) -> dict:
        """Statusabbild für /api/command/status."""
        return {
            "available": SERIAL_AVAILABLE,
            "connected": self.is_running(),
            "port":      self.port,
            "baud":      self.baud,
            "error":     self.error,
        }

    # -- Intern -------------------------------------------------------------
    def _open_locked(self) -> None:
        """Öffnet self.port neu. Erwartet self._lock. Wirft RuntimeError."""
        self._close_locked()
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.2)
        except (OSError, serial.SerialException) as exc:
            self.error = str(exc)
            self._ser = None
            self._node = None
            raise RuntimeError(f"TC-Port {self.port} nicht offen: {exc}") from exc
        self._node = self._stat_node()
        self.error = None

    def _write_locked(self, packet: bytes) -> None:
        """Schreibt ein Paket. Erwartet self._lock."""
        if not (self._ser and self._ser.is_open):
            raise serial.SerialException("TC-Port nicht offen")
        self._ser.write(packet)
        self._ser.flush()

    def _stat_node(self) -> Optional[tuple]:
        """Identität des Device-Nodes (Gerät + Inode) — None, wenn es ihn nicht gibt."""
        try:
            st = os.stat(self.port)
        except OSError:
            return None
        return (st.st_rdev, st.st_ino)

    def _node_alive(self) -> bool:
        """
        True, solange der geöffnete Device-Node noch derselbe ist.

        Nach einem Neu-Enumerieren des USB-Adapters legt macOS einen neuen Node
        an; unser fd zeigt dann ins Leere, obwohl pyserial ihn für offen hält.
        """
        return self._node is not None and self._stat_node() == self._node

    def _close_locked(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ser = None
        self._node = None


# Modul-Singleton — geteilt zwischen REST-Routes.
tc = TcUplink()


def init_tc_uplink(app) -> None:
    """
    Initialisiert den TC-Uplink aus der App-Config und verbindet ggf. automatisch.

    Einmalig in create_app() aufrufen.
    """
    if not SERIAL_AVAILABLE:
        log.info("TC-Uplink deaktiviert (pyserial nicht installiert)")
        return

    port = (app.config.get("TC_SERIAL_PORT") or "").strip()
    baud = int(app.config.get("TC_SERIAL_BAUD", 38400))
    autostart = bool(app.config.get("TC_AUTOSTART", True))

    if port and autostart:
        try:
            tc.start(port, baud)
        except RuntimeError as exc:
            log.warning("TC-Uplink-Autostart fehlgeschlagen: %s", exc)
    else:
        log.info("TC-Uplink im Standby (kein Port konfiguriert — "
                 "Port zur Laufzeit über /api/command/connect wählen)")

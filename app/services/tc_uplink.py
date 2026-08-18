"""
MAGGIE – RXSM Telecommand Uplink (serieller TX)
================================================

Sendet Telecommands an das RXSM (im Test das REXUS Test Module, im Experiment
das REXUS Service Module) über einen SEPARATEN seriellen Port. Das RXSM leitet
die SDC-Nutzbytes über die UART an den OBC weiter.

Bewusst getrennt vom read-only Downlink-Reader (serial_listener.py):
  - andere Richtung (TX statt RX), anderer Port, eigener Lebenszyklus
  - eigener serial.Serial-Handle + Lock für thread-sicheres Schreiben

Frame-Encoder: rxsm_tc_parser.build_sdc_packet() (24-Byte RXSM-TC, SDC=0xA5).

Konfiguration (config/settings.py / .env):
  TC_SERIAL_PORT       Portname zum RXSM-(Test-)Modul (leer → zur Laufzeit wählen)
  TC_SERIAL_BAUD       Baudrate (RXSM-Standard: 38400)
  TC_AUTOSTART         beim Start automatisch verbinden (wenn Port gesetzt)
"""

from __future__ import annotations

import logging
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
        self.port = ""
        self.baud = 38400
        self.error: Optional[str] = None

    # -- Öffentliche API ----------------------------------------------------
    def start(self, port: str, baud: int) -> None:
        """(Neu-)Öffnet den seriellen TC-Port."""
        if not SERIAL_AVAILABLE:
            raise RuntimeError("pyserial nicht installiert")
        with self._lock:
            self._close_locked()
            try:
                self._ser = serial.Serial(port, int(baud), timeout=0.2)
            except (OSError, serial.SerialException) as exc:
                self.error = str(exc)
                self._ser = None
                raise RuntimeError(f"TC-Port {port} nicht offen: {exc}") from exc
            self.port = port
            self.baud = int(baud)
            self.error = None
        log.info("RXSM-TC-Uplink verbunden: %s @ %d Baud", port, baud)

    def stop(self) -> None:
        with self._lock:
            self._close_locked()
        log.info("RXSM-TC-Uplink getrennt")

    def is_running(self) -> bool:
        return self._ser is not None and self._ser.is_open

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
            if not (self._ser and self._ser.is_open):
                raise RuntimeError("TC-Uplink nicht verbunden")
            mcnt = self._mcnt
            self._mcnt = (self._mcnt + 1) & 0xFF
            packet = build_sdc_packet(mcnt=mcnt, dest=dest, payload=payload)
            try:
                self._ser.write(packet)
                self._ser.flush()
            except (OSError, serial.SerialException) as exc:
                self.error = str(exc)
                raise RuntimeError(f"TC-Write fehlgeschlagen: {exc}") from exc

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
    def _close_locked(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ser = None


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

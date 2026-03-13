"""
MAGGIE – Command Service (Server → OBC)
=========================================

Sendet Befehle per UDP an den OBC (Teensy 4.1).

Befehlsformat (Big-Endian, 16 Bytes fest):
┌─────────────────────────────────────────────────────────────┐
│  [0:2]   Magic     0x4D43 ('MC' = MAGGIE Command) (uint16)  │
│  [2:4]   Sequence  Command-Zähler                  (uint16) │
│  [4:5]   CmdID     Befehls-Typ (siehe unten)       (uint8)  │
│  [5:6]   Param1    1. Parameter                     (uint8) │
│  [6:8]   Param2    2. Parameter (16 Bit)            (uint16)│
│  [8:12]  Payload   Optionaler 32-Bit-Wert           (uint32)│
│  [12:14] Reserved  Reserviert                       (uint16)│
│  [14:16] CRC16     XModem-CRC über [0:14]           (uint16)│
└─────────────────────────────────────────────────────────────┘

CmdID-Werte:
  0x01  CMD_SET_STATE      — Mission-State setzen  (Param1 = State-Index)
  0x02  CMD_HRDM_DEPLOY    — HRDM auslösen
  0x03  CMD_HRDM_LOCK      — HRDM verriegeln
  0x04  CMD_LIGHT_ON       — Light Unit einschalten
  0x05  CMD_LIGHT_OFF      — Light Unit ausschalten
  0x06  CMD_ARM_ESTOP      — Roboterarm Emergency Stop
  0x07  CMD_SELECT_MODE    — Operation Mode wählen (Param1 = Mode-Index)
"""

from __future__ import annotations

import struct
import socket
import logging
import threading
from typing import Optional

from flask import Flask

log = logging.getLogger(__name__)

# ── Protokoll-Konstanten ─────────────────────────────────────────────────────

CMD_MAGIC       = 0x4D43   # 'MC'
CMD_PACKET_SIZE = 16
CMD_FORMAT      = ">HHBBHIH"   # magic(2) seq(2) cmd(1) p1(1) p2(2) payload(4) rsv(2) crc(2) = 16 Bytes

# CmdID-Werte
CMD_SET_STATE    = 0x01
CMD_HRDM_DEPLOY  = 0x02
CMD_HRDM_LOCK    = 0x03
CMD_LIGHT_ON     = 0x04
CMD_LIGHT_OFF    = 0x05
CMD_ARM_ESTOP    = 0x06
CMD_SELECT_MODE  = 0x07

# State-Index → Name (Reihenfolge muss mit OBC-Enum übereinstimmen)
STATE_INDEX = {
    "STARTUP":          0,
    "PREFLIGHT_CHECK":  1,
    "STANDBY":          2,
    "EXPERIMENT":       3,
    "TESTING":          4,
    "HARDWARE_TESTING": 5,
    "SHUTDOWN":         6,
}

MODE_INDEX = {
    "NONE":           0,
    "FLIGHT":         1,
    "TESTING":        2,
    "HARDWARE_TEST":  3,
}


# ── CRC-16 / XModem ─────────────────────────────────────────────────────────

def _crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM — gleiche Implementierung wie auf dem OBC."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# ── Command Service ──────────────────────────────────────────────────────────

class CommandService:
    """
    Sendet UDP-Befehle an den OBC.

    Verwendung::

        from app.services.command_service import cmd_service
        cmd_service.init(app)
        cmd_service.send_set_state("EXPERIMENT")
        cmd_service.send_hrdm_deploy()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq: int = 0
        self._obc_host: str = "192.168.1.10"
        self._obc_port: int = 9001
        self._sock: Optional[socket.socket] = None

    def init(self, app: Flask) -> None:
        """Liest Config und erstellt den UDP-Socket."""
        self._obc_host = app.config.get("OBC_CMD_HOST", "192.168.1.10")
        self._obc_port = int(app.config.get("OBC_CMD_PORT", 9001))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info(
            "Command-Service initialisiert → %s:%d",
            self._obc_host, self._obc_port,
        )

    # ── Öffentliche Befehle ───────────────────────────────────────

    def send_set_state(self, state: str) -> bool:
        """Sendet CMD_SET_STATE mit dem State-Index."""
        idx = STATE_INDEX.get(state.upper())
        if idx is None:
            log.error("Unbekannter State: %s", state)
            return False
        return self._send(CMD_SET_STATE, param1=idx)

    def send_select_mode(self, mode: str) -> bool:
        """Sendet CMD_SELECT_MODE mit dem Mode-Index."""
        idx = MODE_INDEX.get(mode.upper())
        if idx is None:
            log.error("Unbekannter Mode: %s", mode)
            return False
        return self._send(CMD_SELECT_MODE, param1=idx)

    def send_hrdm_deploy(self) -> bool:
        """Sendet CMD_HRDM_DEPLOY."""
        return self._send(CMD_HRDM_DEPLOY)

    def send_hrdm_lock(self) -> bool:
        """Sendet CMD_HRDM_LOCK."""
        return self._send(CMD_HRDM_LOCK)

    def send_light_on(self) -> bool:
        """Sendet CMD_LIGHT_ON."""
        return self._send(CMD_LIGHT_ON)

    def send_light_off(self) -> bool:
        """Sendet CMD_LIGHT_OFF."""
        return self._send(CMD_LIGHT_OFF)

    def send_arm_estop(self) -> bool:
        """Sendet CMD_ARM_ESTOP (Emergency Stop Roboterarm)."""
        return self._send(CMD_ARM_ESTOP)

    # ── Intern ────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFF
            return self._seq

    def _build_packet(
        self,
        cmd_id: int,
        param1: int = 0,
        param2: int = 0,
        payload: int = 0,
    ) -> bytes:
        """Baut ein 16-Byte Command-Paket inkl. CRC."""
        seq = self._next_seq()
        reserved = 0

        # Ohne CRC packen (14 Bytes)
        header = struct.pack(
            ">HHBBHI",
            CMD_MAGIC, seq, cmd_id, param1, param2, payload,
        )
        # Reserved anhängen
        header += struct.pack(">H", reserved)

        # CRC über die ersten 14 Bytes
        crc = _crc16_xmodem(header[:14])

        # Vollständiges Paket (14 + 2 = 16 Bytes)
        packet = header[:14] + struct.pack(">H", crc)

        assert len(packet) == CMD_PACKET_SIZE, f"Paket hat {len(packet)} Bytes statt {CMD_PACKET_SIZE}"
        return packet

    def _send(
        self,
        cmd_id: int,
        param1: int = 0,
        param2: int = 0,
        payload: int = 0,
    ) -> bool:
        """Baut und sendet ein Command-Paket an den OBC."""
        if self._sock is None:
            log.error("Command-Service nicht initialisiert")
            return False

        packet = self._build_packet(cmd_id, param1, param2, payload)

        try:
            self._sock.sendto(packet, (self._obc_host, self._obc_port))
            log.info(
                "CMD gesendet: id=0x%02X p1=%d p2=%d → %s:%d (seq=%d)",
                cmd_id, param1, param2,
                self._obc_host, self._obc_port,
                self._seq,
            )
            return True
        except OSError as exc:
            log.error("CMD senden fehlgeschlagen: %s", exc)
            return False


# ── Modul-Level Singleton ────────────────────────────────────────────────────
cmd_service = CommandService()

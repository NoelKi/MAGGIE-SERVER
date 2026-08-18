"""
MAGGIE – OBC Downlink Frame Parser (20-Byte MAGGIE-Downlink)
============================================================

Decodiert das feste 20-Byte-Downlink-Frame, das der OBC (Teensy 4.1, Serial8)
über den seriellen RXSM-Downlink sendet.

Ground-Truth: MAGGIE_OBC/include/hal/telemetry_hal.hpp + telemetry_hal.cpp

Frame (20 Bytes, big-endian):
  Idx  Feld     Bytes  Beschreibung
  ───  ───────  ─────  ─────────────────────────────────────────────
   0   START     1     Frame-Start-Marker (DL_START = 0x7E)
   1   MSGID1    1     Subsystem / Kategorie (DownlinkSubsystem)
   2   MSGID2    1     Nachrichtentyp im Subsystem
   3   ACK       1     Acknowledge-Flag (0 bei reiner Telemetrie)
  4-5  COUNTER   2     Frame-Zähler, big-endian
  6-7  TIME      2     On-Board-Zeit, big-endian (untere 16 Bit von millis())
 8-15  DATA      8     Nutzdaten, Layout je MSGID (big-endian)
  16   STATUS1   1     Status-Bitfeld 1
  17   STATUS2   1     Status-Bitfeld 2
  18   CRC       1     CRC-8 (poly 0x07, init 0x00) über Bytes 1..17
  19   END       1     Frame-End-Marker (DL_END = 0x7F)

DATA-Layout:
  IMU/ACCEL: [ax ay az 0 0]  (3× int16 BMI088-Counts)
  IMU/GYRO : [gx gy gz 0 0]  (3× int16 BMI088-Counts)

Skalierung (Bodenstation):
  accel [m/s²] = count · 9.80665 / 10920   (±3 g)
  gyro  [°/s]  = count · 1 / 65.536         (±500 °/s)
"""

from __future__ import annotations

import time
import struct
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Protokoll-Konstanten (aus telemetry_hal.hpp) ────────────────────────────────
FRAME_SIZE = 20
DATA_SIZE  = 8

DL_START = 0x7E
DL_END   = 0x7F

# MSGID1 – Subsystem
SUBSYS_IMU   = 0x01
SUBSYS_MOTOR = 0x02

# MSGID2 – Nachrichtentyp (IMU-Subsystem)
IMU_ACCEL = 0x01
IMU_GYRO  = 0x02

# MSGID2 – Nachrichtentyp (MOTOR-Subsystem)
MOTOR_STATE = 0x01

# STATUS1-Bits
STATUS1_SYSTEM_HEALTHY = 0x01
STATUS1_IMU_VALID      = 0x02

# MOTOR-STATE-Bits (DATA[6], identisch zu telemetry_hal.hpp)
MOTOR_STATE_ON        = 0x01
MOTOR_STATE_MOVING    = 0x02
MOTOR_STATE_AT_TARGET = 0x04

# Motor-Positionsskalierung (identisch zur OBC-Firmware motor_hal.hpp)
MOTOR_COUNTS_PER_REV = 4600.0    # Encoder-Counts pro voller Umdrehung

# Skalierungsfaktoren (identisch zur OBC-Firmware imu_hal.cpp)
ACCEL_SCALE = 9.80665 / 10920.0    # int16-Count → m/s²
GYRO_SCALE  = 1.0 / 65.536         # int16-Count → °/s


# ── Datenklasse ─────────────────────────────────────────────────────────────────
@dataclass
class DownlinkFrame:
    msgid1:   int
    msgid2:   int
    ack:      int
    counter:  int
    time_ms:  int
    status1:  int
    status2:  int
    crc:      int
    data:     bytes
    fields:   dict = field(default_factory=dict)   # decodierte, skalierte Werte
    valid:    bool = True
    error:    Optional[str] = None

    @property
    def _mtype(self) -> "Optional[MessageType]":
        return MESSAGE_TYPES.get((self.msgid1, self.msgid2))

    @property
    def subsystem_name(self) -> str:
        mt = self._mtype
        return mt.subsystem if mt else f"unknown_0x{self.msgid1:02X}"

    @property
    def type_name(self) -> str:
        mt = self._mtype
        return mt.name if mt else f"0x{self.msgid2:02X}"

    @property
    def measurement(self) -> Optional[str]:
        """InfluxDB-Measurement — None wenn der Typ nicht registriert ist."""
        mt = self._mtype
        return mt.measurement if mt else None

    @property
    def system_healthy(self) -> bool:
        return bool(self.status1 & STATUS1_SYSTEM_HEALTHY)

    @property
    def imu_valid(self) -> bool:
        return bool(self.status1 & STATUS1_IMU_VALID)

    def to_dict(self) -> dict:
        """JSON-freundliche Repräsentation für die GUI."""
        return {
            "counter":        self.counter,
            "time_ms":        self.time_ms,
            "subsystem":      self.subsystem_name,
            "type":           self.type_name,
            "measurement":    self.measurement,
            "ack":            self.ack,
            "status1":        self.status1,
            "status2":        self.status2,
            "system_healthy": self.system_healthy,
            "imu_valid":      self.imu_valid,
            "crc":            self.crc,
            "fields":         self.fields,
        }


# ── CRC-8 (poly 0x07, init 0x00) ────────────────────────────────────────────────
def crc8(data: bytes) -> int:
    """CRC-8/SMBUS — identisch zur OBC-Firmware (telemetry_hal.cpp)."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


# ── Message-Registry (skalierbar) ───────────────────────────────────────────────
#
# Ein neuer Downlink-Wert = eine Decoder-Funktion + ein register_message()-Aufruf.
# Alles Weitere (Namen, InfluxDB-Measurement, GUI-Frames) leitet sich automatisch
# daraus ab — es muss an KEINER anderen Stelle etwas geändert werden.
#
# Decoder-Signatur:  (data: bytes[8]) -> dict[str, float]   (skalierte Messwerte)

Decoder = Callable[[bytes], dict]


@dataclass(frozen=True)
class MessageType:
    subsystem:   str          # Kategorie-Name, z.B. "imu"
    name:        str          # Typ-Name, z.B. "accel"
    measurement: str          # InfluxDB-Measurement, z.B. "imu"
    decoder:     Decoder      # DATA-Bytes → skalierte Felder


# (MSGID1, MSGID2) → MessageType
MESSAGE_TYPES: dict[tuple[int, int], MessageType] = {}


def register_message(msgid1: int, msgid2: int, subsystem: str, name: str,
                     measurement: str, decoder: Decoder) -> None:
    """Registriert einen Downlink-Nachrichtentyp. Beim Import einmalig aufrufen."""
    MESSAGE_TYPES[(msgid1, msgid2)] = MessageType(subsystem, name, measurement, decoder)


# ── Decoder-Funktionen ───────────────────────────────────────────────────────────
def _decode_imu_accel(data: bytes) -> dict:
    ax, ay, az = struct.unpack_from(">hhh", data)
    return {
        "ax": round(ax * ACCEL_SCALE, 4),
        "ay": round(ay * ACCEL_SCALE, 4),
        "az": round(az * ACCEL_SCALE, 4),
    }


def _decode_imu_gyro(data: bytes) -> dict:
    gx, gy, gz = struct.unpack_from(">hhh", data)
    return {
        "gx": round(gx * GYRO_SCALE, 4),
        "gy": round(gy * GYRO_SCALE, 4),
        "gz": round(gz * GYRO_SCALE, 4),
    }


def _decode_motor(data: bytes) -> dict:
    # DATA: [pos(int32 BE) pwm(int16 BE) state(uint8) spare]
    # 'pwm' = vorzeichenbehafteter PWM-Wert (-255..255).
    position, pwm, state, _spare = struct.unpack_from(">ihBB", data)
    return {
        "position":    position,
        "revolutions": round(position / MOTOR_COUNTS_PER_REV, 4),
        "pwm":         pwm,
        "on":          bool(state & MOTOR_STATE_ON),
        "moving":      bool(state & MOTOR_STATE_MOVING),
        "at_target":   bool(state & MOTOR_STATE_AT_TARGET),
    }


# ── Registrierte Nachrichtentypen ─────────────────────────────────────────────────
register_message(SUBSYS_IMU, IMU_ACCEL, "imu", "accel", "imu", _decode_imu_accel)
register_message(SUBSYS_IMU, IMU_GYRO,  "imu", "gyro",  "imu", _decode_imu_gyro)
register_message(SUBSYS_MOTOR, MOTOR_STATE, "motor", "state", "motor", _decode_motor)

# NEUEN WERT HINZUFÜGEN — Beispiel (später, wenn der OBC z.B. Wiegezellen sendet):
#
#   1) MSGID-Konstanten oben ergänzen, passend zur OBC-Firmware:
#        SUBSYS_LOADCELL = 0x02
#        LOADCELL_TARGET1 = 0x01
#   2) Decoder schreiben (DATA-Bytes → skalierte Felder):
#        def _decode_loadcell(data: bytes) -> dict:
#            fx, fy, fz = struct.unpack_from(">hhh", data)
#            return {"force_x": round(fx * LOADCELL_SCALE, 3), ...}
#   3) Registrieren:
#        register_message(SUBSYS_LOADCELL, LOADCELL_TARGET1,
#                         "loadcell", "target1", "load_cells", _decode_loadcell)
#
# Danach wird der Typ automatisch decodiert, ins Measurement "load_cells"
# geschrieben und im Downlink-Monitor angezeigt — ohne weitere Codeänderung.


def _decode_fields(msgid1: int, msgid2: int, data: bytes) -> dict:
    """Decodiert die 8 DATA-Bytes über die Registry (unbekannt → Rohbytes)."""
    mt = MESSAGE_TYPES.get((msgid1, msgid2))
    if mt is None:
        return {"raw_hex": data.hex()}
    return mt.decoder(data)


def parse_frame(raw: bytes) -> DownlinkFrame:
    """
    Parst ein 20-Byte-Frame. Bei Fehler: valid=False, error gesetzt.

    Prüft START-/END-Marker und CRC-8.
    """
    def _bad(error: str, **kw) -> DownlinkFrame:
        return DownlinkFrame(
            msgid1=kw.get("msgid1", 0), msgid2=kw.get("msgid2", 0), ack=0,
            counter=0, time_ms=0, status1=0, status2=0, crc=0, data=b"",
            valid=False, error=error,
        )

    if len(raw) != FRAME_SIZE:
        return _bad(f"Falsche Länge: {len(raw)} (erwartet {FRAME_SIZE})")
    if raw[0] != DL_START:
        return _bad(f"START falsch: 0x{raw[0]:02X}")
    if raw[19] != DL_END:
        return _bad(f"END falsch: 0x{raw[19]:02X}")

    msgid1, msgid2, ack = raw[1], raw[2], raw[3]
    counter  = (raw[4] << 8) | raw[5]
    time_ms  = (raw[6] << 8) | raw[7]
    data     = bytes(raw[8:16])
    status1, status2 = raw[16], raw[17]
    crc      = raw[18]

    computed = crc8(raw[1:18])   # Bytes 1..17
    if computed != crc:
        f = _bad(f"CRC-Fehler: 0x{computed:02X} != 0x{crc:02X}",
                 msgid1=msgid1, msgid2=msgid2)
        f.error = "crc"          # normierter Fehlercode für Zähler
        return f

    return DownlinkFrame(
        msgid1=msgid1, msgid2=msgid2, ack=ack,
        counter=counter, time_ms=time_ms,
        status1=status1, status2=status2, crc=crc, data=data,
        fields=_decode_fields(msgid1, msgid2, data),
    )


# ── Stream-Extractor (Byte-Strom → Frames) ──────────────────────────────────────
class FrameExtractor:
    """
    Zerlegt einen kontinuierlichen Byte-Strom in gültige 20-Byte-Frames.

    Sync-Strategie: nach DL_START suchen, Frame-Länge prüfen, dann per
    END-Marker + CRC verifizieren. Bei ungültigem Kandidaten wird um ein
    Byte weitergerückt (Resync); Teil-Frames am Ende bleiben gepuffert.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.crc_errors = 0        # Frames mit korrekten Markern aber falscher CRC
        self.resync_bytes = 0      # verworfene Bytes bei Resync

    def feed(self, data: bytes) -> list[DownlinkFrame]:
        self._buf.extend(data)
        buf = self._buf
        frames: list[DownlinkFrame] = []
        pos = 0
        n = len(buf)

        while True:
            start = buf.find(DL_START, pos)
            if start < 0:
                pos = n                      # kein Startmarker mehr
                break
            if n - start < FRAME_SIZE:
                pos = start                  # Teil-Frame → für nächstes feed behalten
                break

            frame = parse_frame(bytes(buf[start:start + FRAME_SIZE]))
            if frame.valid:
                frames.append(frame)
                pos = start + FRAME_SIZE
            else:
                if frame.error == "crc":
                    self.crc_errors += 1
                self.resync_bytes += 1
                pos = start + 1              # ein Byte weiter, neu synchronisieren

        if pos:
            del buf[:pos]
        return frames


# ── Thread-sicherer Speicher für decodierte Frames (GUI-Poll) ───────────────────
class DecodedStore:
    """Hält die zuletzt decodierten Frames + Zähler für die GUI."""

    def __init__(self, maxlen: int = 300) -> None:
        import threading
        self._lock = threading.Lock()
        self._frames: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self.total = 0
        self.last_counter: Optional[int] = None

    def add(self, frame: DownlinkFrame) -> None:
        with self._lock:
            self._seq += 1
            self.total += 1
            self.last_counter = frame.counter
            entry = frame.to_dict()
            entry["seq"] = self._seq
            entry["ts"] = int(time.time() * 1000)   # Empfangszeit (epoch ms) für Live-Charts
            self._frames.append(entry)

    def since(self, since: int) -> tuple[list[dict], int]:
        with self._lock:
            if since < 0 or since > self._seq:
                return [], self._seq
            out = [f for f in self._frames if f["seq"] > since]
            return out, self._seq

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "frames_total": self.total,
                "last_counter": self.last_counter,
                "cursor":       self._seq,
            }

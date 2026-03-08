"""
MAGGIE – OBC Packet Parser
==========================

Definiert das binäre Paketformat, das der OBC über UDP an den
MAGGIE Ground-Station-Server schickt.

KONTEXT — REXUS-Kommunikationsarchitektur:
─────────────────────────────────────────────────────────────────
  BODEN ──[TC 60 Hz GMSK]──► RXSM ──[UART]──► OBC (Experiment)
  BODEN ◄─[TM downlink]───── RXSM ◄─[UDP/ETH]─ OBC (Experiment)

  Der OBC EMPFÄNGT vom RXSM via UART:
    • SODS-Signal (Start of Data Storage)
    • SOEX-Signal (Start of Experiment)
    • Telecommands (MSGID=$A5, SDC-Type) als serielle UART-Daten

  Der OBC SENDET an die Ground Station via Ethernet/UDP:
    • Telemetrie-Pakete im MAGGIE-Protokoll (dieses Format hier)

  Das RXSM-TC-Format (24 Bytes GMSK, 60 Hz):
    [SYNC1][SYNC2][MSGID][MCNT][Data 0-15][CSM][CSM][CRC][CRC]
    MSGID=$00 → SMC: Power/SODS/SOEX/Status-Bits
    MSGID=$A5 → SDC: Serial Data Command → UART an Experiment

  Die SODS/SOEX/LiftOff-Flags im MAGGIE-Header unten werden vom
  OBC gesetzt, nachdem er sie über seinen RXSM-UART-Kanal empfangen hat.
─────────────────────────────────────────────────────────────────

MAGGIE PAKETSTRUKTUR (Big-Endian, gesamt 64 Bytes fest):
┌─────────────────────────────────────────────────────────────┐
│ HEADER  (16 Bytes)                                          │
│  [0:2]   Magic          0x4D41 ('MA')           (uint16)    │
│  [2:4]   Version        0x0001                  (uint16)    │
│  [4:8]   Sequence       Paket-Zähler seit Boot  (uint32)    │
│  [8:12]  Timestamp      ms seit Lift-off         (uint32)   │
│  [12:13] Type           Measurement-ID           (uint8)    │
│  [13:14] Flags          RXSM-Signale (siehe unten)(uint8)   │
│  [14:16] CRC16          XModem-CRC über [0:14]   (uint16)  │
├─────────────────────────────────────────────────────────────┤
│ PAYLOAD (48 Bytes)  — abhängig von Type                     │
│  TYPE 0x01  IMU         6× float32  (ax ay az gx gy gz)    │
│  TYPE 0x02  ENVIRONMENT 3× float32  (temp pressure humidity)│
│  TYPE 0x03  GPS         4× float32  (lat lon alt speed)     │
│  TYPE 0x04  SYSTEM      4× float32  (cpu_temp bat_v bat_i  │
│                                      uptime_s)              │
│  TYPE 0xFF  HEARTBEAT   1× uint32   (boot_count)            │
│  unbekannte Types: Payload wird als Rohbytes gespeichert    │
└─────────────────────────────────────────────────────────────┘

Flag-Byte (Bit-Mapping nach RXSM SMC MSGID=$00):
  Bit 0 (0x01) : Lift-Off erkannt         (RXSM LO-Pin / Lift-off pins)
  Bit 1 (0x02) : SODS aktiv               (RXSM SMC Byte 1, aus TC-Strom)
  Bit 2 (0x04) : SOEX aktiv               (RXSM SMC Byte 2, aus TC-Strom)
  Bit 3 (0x08) : Motor-Burnout            (OBC-eigene Erkennung via IMU)
  Bit 4 (0x10) : Apogee erreicht          (OBC-eigene Erkennung via Baro/IMU)
  Bit 5 (0x20) : Parachute deployed       (OBC-eigene Erkennung via Baro)

C-Struct für den OBC:
  #pragma pack(push, 1)
  typedef struct {
      uint16_t magic;          // 0x4D41  ('MA')
      uint16_t version;        // 0x0001
      uint32_t sequence;       // Paket-Zähler seit Boot
      uint32_t timestamp_ms;   // ms seit Lift-off (LO-Erkennung = t=0)
      uint8_t  type;           // Measurement-Type
      uint8_t  flags;          // RXSM-Signale (Bit-Feld oben)
      uint16_t crc16;          // CRC-16/XModem über Bytes 0..13
  } MaggieHeader;              // = 16 Bytes
  #pragma pack(pop)
"""

import struct
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── Konstanten ────────────────────────────────────────────────────────────────
MAGIC           = 0x4D41          # 'MA'
PROTOCOL_VER    = 0x0001
HEADER_FORMAT   = ">HHIIBBH"     # Big-Endian: magic(2) version(2) seq(4) ts(4) type(1) flags(1) crc16(2) = 16 Bytes
HEADER_SIZE     = struct.calcsize(HEADER_FORMAT)   # 16 Bytes

PAYLOAD_SIZE    = 48
PACKET_SIZE     = HEADER_SIZE + PAYLOAD_SIZE       # 64 Bytes gesamt

# Measurement-Type-IDs
TYPE_IMU         = 0x01
TYPE_ENVIRONMENT = 0x02
TYPE_GPS         = 0x03
TYPE_SYSTEM      = 0x04
TYPE_HEARTBEAT   = 0xFF

# Flag-Bits
FLAG_LIFTOFF    = 0x01
FLAG_SODS       = 0x02
FLAG_SOEX       = 0x04
FLAG_BURNOUT    = 0x08
FLAG_APOGEE     = 0x10
FLAG_PARACHUTE  = 0x20


# ── Datenklassen ─────────────────────────────────────────────────────────────
@dataclass
class RexusHeader:
    magic:        int
    version:      int
    sequence:     int
    timestamp_ms: int
    pkt_type:     int
    flags:        int
    crc16:        int

    @property
    def type_name(self) -> str:
        """Gibt den Measurement-Typ als lesbaren String zurück (z.B. 'imu', 'gps')."""
        return {
            TYPE_IMU:         "imu",
            TYPE_ENVIRONMENT: "environment",
            TYPE_GPS:         "gps",
            TYPE_SYSTEM:      "system",
            TYPE_HEARTBEAT:   "heartbeat",
        }.get(self.pkt_type, f"unknown_0x{self.pkt_type:02X}")

    @property
    def flight_phase(self) -> str:
        """Leitet die Flugphase aus den Flag-Bits ab."""
        if self.flags & FLAG_PARACHUTE:
            return "descent"
        if self.flags & FLAG_APOGEE:
            return "apogee"
        if self.flags & FLAG_BURNOUT:
            return "coasting"
        if self.flags & FLAG_SOEX:
            return "experiment"
        if self.flags & FLAG_SODS:
            return "sods"
        if self.flags & FLAG_LIFTOFF:
            return "ascent"
        return "ground"


@dataclass
class RexusPacket:
    header:  RexusHeader
    fields:  dict = field(default_factory=dict)
    raw_payload: bytes = field(default_factory=bytes)
    valid:   bool = True
    error:   Optional[str] = None


# ── CRC-16 / XModem ──────────────────────────────────────────────────────────
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


# ── Payload-Parser ────────────────────────────────────────────────────────────
def _parse_imu(payload: bytes) -> dict:
    """6× float32: ax, ay, az [m/s²]  gx, gy, gz [°/s]"""
    ax, ay, az, gx, gy, gz = struct.unpack_from(">ffffff", payload)
    return {
        "ax": round(ax, 4), "ay": round(ay, 4), "az": round(az, 4),
        "gx": round(gx, 4), "gy": round(gy, 4), "gz": round(gz, 4),
    }


def _parse_environment(payload: bytes) -> dict:
    """3× float32: temperature [°C], pressure [hPa], humidity [%RH]"""
    temp, pressure, humidity = struct.unpack_from(">fff", payload)
    return {
        "temperature": round(temp, 3),
        "pressure":    round(pressure, 3),
        "humidity":    round(humidity, 3),
    }


def _parse_gps(payload: bytes) -> dict:
    """4× float32: latitude [°], longitude [°], altitude [m], speed [m/s]"""
    lat, lon, alt, speed = struct.unpack_from(">ffff", payload)
    return {
        "latitude":  round(lat, 6),
        "longitude": round(lon, 6),
        "altitude":  round(alt, 2),
        "speed":     round(speed, 3),
    }


def _parse_system(payload: bytes) -> dict:
    """4× float32: cpu_temp [°C], battery_voltage [V], battery_current [mA],
    uptime_s [s]"""
    cpu_temp, bat_v, bat_i, uptime = struct.unpack_from(">ffff", payload)
    return {
        "cpu_temp":         round(cpu_temp, 2),
        "battery_voltage":  round(bat_v, 3),
        "battery_current":  round(bat_i, 3),
        "uptime_s":         round(uptime, 1),
    }


def _parse_heartbeat(payload: bytes) -> dict:
    """1× uint32: boot_count"""
    (boot_count,) = struct.unpack_from(">I", payload)
    return {"boot_count": boot_count}


_PAYLOAD_PARSERS = {
    TYPE_IMU:         _parse_imu,
    TYPE_ENVIRONMENT: _parse_environment,
    TYPE_GPS:         _parse_gps,
    TYPE_SYSTEM:      _parse_system,
    TYPE_HEARTBEAT:   _parse_heartbeat,
}


# ── Öffentliche API ───────────────────────────────────────────────────────────
def parse_packet(raw: bytes) -> RexusPacket:
    """
    Nimmt rohe UDP-Nutzdaten entgegen und gibt ein RexusPacket zurück.

    Bei Fehler ist packet.valid == False und packet.error enthält die Ursache.
    """
    if len(raw) < PACKET_SIZE:
        empty = RexusHeader(magic=0, version=0, sequence=0,
                            timestamp_ms=0, pkt_type=0, flags=0, crc16=0)
        return RexusPacket(
            header=empty,
            valid=False,
            error=f"Paket zu kurz: {len(raw)} Bytes (erwartet {PACKET_SIZE})",
        )

    # Header entpacken
    magic, version, seq, ts_ms, pkt_type, flags, crc16 = struct.unpack_from(
        HEADER_FORMAT, raw
    )

    header = RexusHeader(
        magic=magic,
        version=version,
        sequence=seq,
        timestamp_ms=ts_ms,
        pkt_type=pkt_type,
        flags=flags,
        crc16=crc16,
    )

    # Magic prüfen
    if magic != MAGIC:
        return RexusPacket(
            header=header, valid=False,
            error=f"Ungültiges Magic: 0x{magic:04X} (erwartet 0x{MAGIC:04X})",
        )

    # CRC prüfen (über erste 14 Bytes = Header ohne die letzten 2 Padding-Bytes
    # und ohne das CRC-Feld selbst)
    computed_crc = _crc16_xmodem(raw[:12] + raw[12:13] + raw[13:14])
    if computed_crc != crc16:
        return RexusPacket(
            header=header, valid=False,
            error=f"CRC-Fehler: berechnet 0x{computed_crc:04X}, "
                  f"erwartet 0x{crc16:04X}",
        )

    # Payload parsen
    payload = raw[HEADER_SIZE: HEADER_SIZE + PAYLOAD_SIZE]
    parser  = _PAYLOAD_PARSERS.get(pkt_type)

    if parser:
        try:
            fields = parser(payload)
        except struct.error as exc:
            return RexusPacket(
                header=header, valid=False,
                error=f"Payload-Parse-Fehler: {exc}",
            )
        return RexusPacket(header=header, fields=fields, raw_payload=payload)
    else:
        # Unbekannter Type — Rohdaten trotzdem speichern
        log.warning("Unbekannter Paket-Type 0x%02X — Rohdaten werden gespeichert", pkt_type)
        return RexusPacket(
            header=header,
            fields={"raw_hex": payload.hex()},
            raw_payload=payload,
        )


def build_packet(
    pkt_type: int,
    sequence: int,
    timestamp_ms: int,
    flags: int,
    payload_fields: dict,
) -> bytes:
    """
    Baut ein gültiges REXUS-Paket für Tests oder OBC-Simulation.

    payload_fields muss zu pkt_type passen:
      TYPE_IMU:         {"ax":..., "ay":..., "az":..., "gx":..., "gy":..., "gz":...}
      TYPE_ENVIRONMENT: {"temperature":..., "pressure":..., "humidity":...}
      TYPE_GPS:         {"latitude":..., "longitude":..., "altitude":..., "speed":...}
      TYPE_SYSTEM:      {"cpu_temp":..., "battery_voltage":...,
                         "battery_current":..., "uptime_s":...}
      TYPE_HEARTBEAT:   {"boot_count":...}
    """
    # Payload bauen
    if pkt_type == TYPE_IMU:
        payload = struct.pack(
            ">ffffff",
            payload_fields["ax"], payload_fields["ay"], payload_fields["az"],
            payload_fields["gx"], payload_fields["gy"], payload_fields["gz"],
        )
    elif pkt_type == TYPE_ENVIRONMENT:
        payload = struct.pack(
            ">fff",
            payload_fields["temperature"],
            payload_fields["pressure"],
            payload_fields["humidity"],
        )
    elif pkt_type == TYPE_GPS:
        payload = struct.pack(
            ">ffff",
            payload_fields["latitude"], payload_fields["longitude"],
            payload_fields["altitude"], payload_fields["speed"],
        )
    elif pkt_type == TYPE_SYSTEM:
        payload = struct.pack(
            ">ffff",
            payload_fields["cpu_temp"], payload_fields["battery_voltage"],
            payload_fields["battery_current"], payload_fields["uptime_s"],
        )
    elif pkt_type == TYPE_HEARTBEAT:
        payload = struct.pack(">I", payload_fields["boot_count"])
    else:
        raise ValueError(f"Unbekannter pkt_type: 0x{pkt_type:02X}")

    # Auf 48 Bytes padden
    payload = payload.ljust(PAYLOAD_SIZE, b'\x00')

    # CRC über Header-Bytes 0..13 (ohne CRC-Feld und Padding)
    header_without_crc = struct.pack(
        ">HHIIBB",
        MAGIC, PROTOCOL_VER, sequence, timestamp_ms, pkt_type, flags,
    )
    crc = _crc16_xmodem(header_without_crc)

    # Vollständigen Header bauen (16 Bytes)
    header = struct.pack(
        ">HHIIBBH",
        MAGIC, PROTOCOL_VER, sequence, timestamp_ms, pkt_type, flags, crc,
    )

    return header + payload

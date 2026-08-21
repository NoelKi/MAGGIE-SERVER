"""
MAGGIE – OBC Downlink Frame Parser (20-Byte MAGGIE-Downlink)
============================================================

Decodiert das feste 20-Byte-Downlink-Frame, das der OBC (Teensy 4.1, Serial4)
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
  IMU/ACCEL:   [ax ay az 0 0]  (3× int16 BMI088-Counts)
  IMU/GYRO :   [gx gy gz 0 0]  (3× int16 BMI088-Counts)
  MOTOR/STATE: [pos(int32) pwm(int16) state(uint8) 0]
  SYS/STATE:   [state(uint8) subsys(uint8) uptime_ms(uint32) rexus(uint8) 0]
  SYS/UPLINK:  [rx_bytes(uint16) frames_ok(uint16) frames_bad(uint16) opcode 0]
  FORCE/TARGET1: [c0 c1 c2 0]    (3× int16: X, Y, Z)
  FORCE/TARGET2: [c0 c1 c2 c3]   (4× int16: A, B, C, D)
                 Flags beider FORCE-Typen stehen in STATUS2, nicht in DATA —
                 TARGET2 braucht alle acht DATA-Bytes für seine vier Zellen.

Skalierung (Bodenstation):
  accel [m/s²] = count · 9.80665 / 10920   (±3 g)
  gyro  [°/s]  = count · 1 / 65.536         (±500 °/s)
  force [N]    = count · FORCE_TELE_DIV / counts_per_gram · 9.80665/1000

Kraftsensor 2 ist ein Eigenbau: Der OBC funkt nur die vier Rohzellen, den
Kraftvektor rechnet erst dieser Parser (siehe FORCE2_CELL_ANGLES_DEG).
"""

from __future__ import annotations

import math
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
SUBSYS_SYS   = 0x03
SUBSYS_FORCE = 0x04

# MSGID2 – Nachrichtentyp (IMU-Subsystem)
IMU_ACCEL = 0x01
IMU_GYRO  = 0x02

# MSGID2 – Nachrichtentyp (MOTOR-Subsystem)
MOTOR_STATE = 0x01

# MSGID2 – Nachrichtentyp (FORCE-Subsystem)
FORCE_TARGET1 = 0x01   # 3 Zellen X/Y/Z
FORCE_TARGET2 = 0x02   # 4 Zellen A/B/C/D (Eigenbau, 3× 120° + Z)

# MSGID2 – Nachrichtentyp (SYS-Subsystem)
SYS_STATE      = 0x01
SYS_UPLINK     = 0x02
SYS_UPLINK_RAW = 0x03

# Die ersten acht Bytes, die ein fehlerfrei uebertragenes SDC-Telecommand
# an der OBC-UART erzeugen muss: SYNC1 SYNC2 MSGID MCNT DEST/LEN START OPCODE ARGHI.
# MCNT und DEST/LEN sind variabel und werden beim Vergleich uebersprungen.
SDC_EXPECTED_HEAD = [0xEB, 0x90, 0xA5, None, None, 0x7E, None, 0x00]

# STATUS1-Bits
STATUS1_SYSTEM_HEALTHY = 0x01
STATUS1_IMU_VALID      = 0x02

# MOTOR-STATE-Bits (DATA[6], identisch zu telemetry_hal.hpp)
# Bit 4 war HDRM_CLOSED der frueheren Positionsregelung und ist mit ihr
# entfallen. Bleibt reserviert.
MOTOR_STATE_ON          = 0x01
MOTOR_STATE_ENCODER_OK  = 0x02
MOTOR_STATE_TURNING     = 0x04
MOTOR_STATE_TURN_FAILED = 0x08

# FORCE-Flags (in STATUS2, identisch zu DL_FORCE_* in telemetry_hal.hpp).
# Ein Sättigungsbit je Kanal, in DATA-Reihenfolge:
#   TARGET1 → bit0 = X, bit1 = Y, bit2 = Z
#   TARGET2 → bit0 = A, bit1 = B, bit2 = C, bit3 = D
FORCE_FLAG_SAT = (0x01, 0x02, 0x04, 0x08)
FORCE_FLAG_TARED = 0x10
FORCE_FLAG_STALE = 0x20

# SYS-STATE-Subsystembits (DATA[1], identisch zu telemetry_hal.hpp)
SUBSYS_BIT_IMU      = 0x01
SUBSYS_BIT_MOTOR    = 0x02
SUBSYS_BIT_DOWNLINK = 0x04
SUBSYS_BIT_FORCE1   = 0x08
SUBSYS_BIT_FORCE2   = 0x10

# Rohe REXUS-Leitungspegel (DATA[6], identisch zu DL_REXUS_* in rexus_hal.hpp)
REXUS_BIT_L0   = 0x01
REXUS_BIT_SOE  = 0x02
REXUS_BIT_SODS = 0x04

# MissionState-Werte (identisch zu MAGGIE_OBC/include/statemachine/mission_state.hpp)
# 1..4 sind fuer die frühere Flugsequenz (ARMED/ASCENT/EXPERIMENT/SAFE)
# reserviert und werden vom aktuellen OBC nicht gesendet.
MISSION_STATES = {
    0: "PRE_LAUNCH",
    5: "ABORT",
    6: "TEST",
}

# Motor-Positionsskalierung (identisch zur OBC-Firmware motor_hal.hpp).
# Am Aufbau bestaetigt; deckt sich mit 12 CPR x 380:1 Getriebe = 4550.
# Muss mit MAGGIE_OBC/include/hal/motor_hal.hpp uebereinstimmen — dort haengt
# ausserdem der Zielwert von MOTOR_TURN daran.
MOTOR_COUNTS_PER_REV = 4550   # Encoder-Counts pro voller Umdrehung
MOTOR_DEG_PER_COUNT  = 360.0 / MOTOR_COUNTS_PER_REV

# Skalierungsfaktoren (identisch zur OBC-Firmware imu_hal.cpp)
ACCEL_SCALE = 9.80665 / 10920.0    # int16-Count → m/s²
GYRO_SCALE  = 1.0 / 65.536         # int16-Count → °/s

# ── Kraftsensoren (HX711) ──────────────────────────────────────────────────────
#
# Teiler, mit dem der OBC die tarierten 24-Bit-Counts ins int16 des Frames
# bringt. MUSS mit FORCE_TELE_DIV in MAGGIE_OBC/include/hal/force_hal.hpp
# uebereinstimmen — wird er dort erhoeht (weil Werte saturieren), gehoert er
# hier mit.
FORCE_TELE_DIV = 1

# Umrechnung Gramm → Newton.
_G_TO_N = 9.80665 / 1000.0

# Kalibrierfaktoren der Wiegezellen in Counts pro Gramm.
#
# BEWUSST HIER UND NICHT IM OBC: Das sind die Werte, die man im Labor zuletzt
# festzurrt (bekannte Masse auflegen, Counts ablesen, teilen). Am Boden sind sie
# ohne Neuflashen aenderbar — genau wie ACCEL_SCALE. Der Nullpunkt dagegen muss
# im OBC abgezogen werden, weil der Rohoffset des HX711 das int16 sprengt.
#
# NOCH NICHT KALIBRIERT: 26.0 ist der Startwert aus
# MAGGIE_OBC/src/hardwareTest/force.cpp und eine Schaetzung. Die '*_counts'-
# Felder im Decoder sind davon unabhaengig und damit der belastbare Wert —
# analog zu 'position' beim Motor.
FORCE1_COUNTS_PER_GRAM = (26.0, 26.0, 26.0)              # X, Y, Z
FORCE2_COUNTS_PER_GRAM = (26.0, 26.0, 26.0, 26.0)        # A, B, C, D

# ── Kraftsensor 2: Geometrie des Eigenbaus ─────────────────────────────────────
#
# Drei Zellen (A, B, C) stehen um 120° versetzt in der XY-Ebene, die vierte (D)
# haengt direkt in Z und ist ohne Verrechnung ablesbar.
#
# Aus den drei radialen Zellen wird der XY-Vektor per CLARKE-TRANSFORMATION —
# dieselbe Mathematik, mit der in der Motorregelung drei Phasen auf zwei Achsen
# abgebildet werden. In amplitudeninvarianter Form:
#
#     Fx = 2/3 · Σ nᵢ·cos(θᵢ)      Fy = 2/3 · Σ nᵢ·sin(θᵢ)
#
# Mit θ = 0°/120°/240° ergibt das geschlossen Fx = (2A−B−C)/3, Fy = (B−C)/√3.
#
# Der Faktor 2/3 macht die Transformation zur Pseudoinversen der Projektion:
# Misst jede Zelle den Anteil der Kraft entlang ihrer eigenen Achse
# (nᵢ = F·cos(φ−θᵢ)), kommt F nach Betrag UND Richtung exakt wieder heraus.
#
# Drei Zellen bei zwei Freiheitsgraden sind statisch ueberbestimmt — es gibt
# also einen Nullraum: Ein Gleichanteil auf allen dreien (A=B=C, z.B. eine
# Montagevorspannung oder Temperaturdrift) faellt bei dieser Rechnung exakt
# heraus und erzeugt keine Scheinkraft. Das ist der Hauptgrund fuer die
# 120°-Anordnung und nicht bloss ein Nebeneffekt.
#
# Umgekehrt heisst das auch: EINE einzeln belastete Zelle ist kein reiner
# Kraftzustand, sondern Kraft plus Vorspannung. Wer am Pruefstand nur an einer
# Zelle zieht, darf also nicht deren vollen Betrag im XY-Vektor erwarten.
#
# WINKEL STATT FESTER ZAHLEN: Wird das Element verdreht eingebaut oder die
# Zellen anders herum verkabelt, ist das hier eine Zeile — mit ausgerechneten
# Koeffizienten waere es eine Fehlersuche. Die Reihenfolge muss zu
# PIN_FORCE2_DOUT in MAGGIE_OBC/include/pin_config.hpp passen.
#
# NOCH NICHT VERMESSEN: 0/120/240 ist die Nennlage. Steht Zelle A nicht auf der
# X-Achse, gehoert der Versatz hier addiert. Pruefen laesst sich das mit einer
# bekannten Last aus einer bekannten Richtung — force2_dir_deg muss dann diese
# Richtung anzeigen.
FORCE2_CELL_ANGLES_DEG = (0.0, 120.0, 240.0)   # A, B, C in der XY-Ebene

# Vorberechnete Clarke-Koeffizienten (cos, sin) je radialer Zelle.
FORCE2_CLARKE = tuple(
    (math.cos(math.radians(a)), math.sin(math.radians(a)))
    for a in FORCE2_CELL_ANGLES_DEG
)


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

Decoder = Callable[[bytes, int], dict]


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
def _decode_imu_accel(data: bytes, status2: int) -> dict:
    ax, ay, az = struct.unpack_from(">hhh", data)
    return {
        "ax": round(ax * ACCEL_SCALE, 4),
        "ay": round(ay * ACCEL_SCALE, 4),
        "az": round(az * ACCEL_SCALE, 4),
    }


def _decode_imu_gyro(data: bytes, status2: int) -> dict:
    gx, gy, gz = struct.unpack_from(">hhh", data)
    return {
        "gx": round(gx * GYRO_SCALE, 4),
        "gy": round(gy * GYRO_SCALE, 4),
        "gz": round(gz * GYRO_SCALE, 4),
    }


def _decode_motor(data: bytes, status2: int) -> dict:
    # DATA: [pos(int32 BE) pwm(int16 BE) state(uint8) spare]
    # 'pwm' = vorzeichenbehafteter PWM-Wert (-255..255).
    position, pwm, state, _spare = struct.unpack_from(">ihBB", data)
    return {
        "position":    position,   # rohe Encoder-Counts, der belastbare Wert
        "revolutions": round(position / MOTOR_COUNTS_PER_REV, 4),
        "angle_deg":   round(position * MOTOR_DEG_PER_COUNT, 2),
        "pwm":         pwm,
        "on":          bool(state & MOTOR_STATE_ON),
        "encoder_ok":  bool(state & MOTOR_STATE_ENCODER_OK),
        "turning":     bool(state & MOTOR_STATE_TURNING),
        "turn_failed": bool(state & MOTOR_STATE_TURN_FAILED),
    }


def _force_channels(data: bytes, status2: int, count: int,
                    counts_per_gram: tuple) -> tuple[list[int], list[float], dict]:
    """
    Gemeinsame Vorarbeit beider FORCE-Typen.

    Zerlegt die DATA-Bytes in `count` tarierte Kanäle und rechnet sie in Newton
    um. Zurück kommen (counts, newton, gemeinsame_flags).
    """
    raw = struct.unpack_from(">" + "h" * count, data)

    counts = [v * FORCE_TELE_DIV for v in raw]
    newton = [c / counts_per_gram[i] * _G_TO_N for i, c in enumerate(counts)]

    flags = {
        # Ohne gueltigen Nullabgleich sind die Werte gegen den Rohoffset des
        # Wandlers gemessen und praktisch bedeutungslos.
        "tared": bool(status2 & FORCE_FLAG_TARED),
        # Wandler haengt: der OBC sendet den letzten Wert weiter (1 Hz), damit
        # die Kurve nicht einfach verschwindet.
        "stale": bool(status2 & FORCE_FLAG_STALE),
    }
    return counts, newton, flags


def _decode_force(data: bytes, status2: int) -> dict:
    """FORCE/TARGET1 — drei Zellen X/Y/Z, keine Verrechnung noetig."""
    counts, newton, flags = _force_channels(data, status2, 3, FORCE1_COUNTS_PER_GRAM)
    axes = ("x", "y", "z")

    out = {}
    for i, axis in enumerate(axes):
        # Counts sind der belastbare Wert: unabhaengig vom noch nicht
        # kalibrierten Faktor. Analog zu 'position' beim Motor.
        out[f"force_{axis}_counts"] = counts[i]
        out[f"force_{axis}"] = round(newton[i], 4)
        # Saturiert = der Wert lag ausserhalb des int16 und ist abgeschnitten.
        # In der Kurve sieht das aus wie ein Plateau, ist aber keins —
        # Gegenmittel: FORCE_TELE_DIV im OBC UND hier erhoehen.
        out[f"force_sat_{axis}"] = bool(status2 & FORCE_FLAG_SAT[i])

    out["force_tared"] = flags["tared"]
    out["force_stale"] = flags["stale"]
    return out


def _decode_force2(data: bytes, status2: int) -> dict:
    """
    FORCE/TARGET2 — vier Rohzellen A/B/C/D des Eigenbaus.

    A, B, C stehen um 120° versetzt in der XY-Ebene, D misst Z direkt. Der
    Kraftvektor entsteht ERST HIER (Clarke-Transformation, siehe
    FORCE2_CELL_ANGLES_DEG) — der OBC funkt bewusst nur die Rohkanaele.
    """
    counts, newton, flags = _force_channels(data, status2, 4, FORCE2_COUNTS_PER_GRAM)
    cells = ("a", "b", "c", "d")

    out = {}
    for i, cell in enumerate(cells):
        out[f"force2_{cell}_counts"] = counts[i]
        out[f"force2_{cell}"] = round(newton[i], 4)
        out[f"force2_sat_{cell}"] = bool(status2 & FORCE_FLAG_SAT[i])

    # Clarke: drei radiale Zellen → XY-Vektor. Amplitudeninvariant (Faktor 2/3),
    # damit eine Kraft genau auf einer Zellenachse als ihr eigener Betrag
    # herauskommt.
    fx = (2.0 / 3.0) * sum(newton[i] * FORCE2_CLARKE[i][0] for i in range(3))
    fy = (2.0 / 3.0) * sum(newton[i] * FORCE2_CLARKE[i][1] for i in range(3))
    fz = newton[3]                      # haengt direkt am Element, keine Rechnung

    out["force2_x"] = round(fx, 4)
    out["force2_y"] = round(fy, 4)
    out["force2_z"] = round(fz, 4)

    # Betrag und Richtung der Querlast. Bei einer 120°-Anordnung ist das die
    # eigentliche Messgroesse — und der Weg, die Winkellage zu pruefen: bekannte
    # Last aus bekannter Richtung auflegen, force2_dir_deg muss sie anzeigen.
    out["force2_xy_mag"] = round(math.hypot(fx, fy), 4)
    out["force2_dir_deg"] = round(math.degrees(math.atan2(fy, fx)), 2)

    # Eine einzige begrenzte Zelle verdreht den GESAMTEN Vektor, nicht nur ihren
    # Kanal — deshalb hier eine Sammelmarkierung, an der die Anzeige haengt.
    out["force2_sat_any"] = any(status2 & FORCE_FLAG_SAT[i] for i in range(4))
    out["force2_tared"] = flags["tared"]
    out["force2_stale"] = flags["stale"]
    return out


def _decode_sys(data: bytes, status2: int) -> dict:
    # DATA: [state(uint8) subsys(uint8) uptime_ms(uint32 BE) rexus(uint8) spare]
    state, subsys, uptime_ms = struct.unpack_from(">BBI", data)
    rexus = data[6] if len(data) > 6 else 0
    return {
        "state":        state,
        "state_name":   MISSION_STATES.get(state, f"UNKNOWN_{state}"),
        "uptime_s":     round(uptime_ms / 1000.0, 1),
        "imu_ok":       bool(subsys & SUBSYS_BIT_IMU),
        "motor_ok":     bool(subsys & SUBSYS_BIT_MOTOR),
        "downlink_ok":  bool(subsys & SUBSYS_BIT_DOWNLINK),
        "force1_ok":    bool(subsys & SUBSYS_BIT_FORCE1),
        "force2_ok":    bool(subsys & SUBSYS_BIT_FORCE2),
        # Rohe REXUS-Leitungspegel (nicht entprellt), siehe DL_REXUS_* im OBC.
        # Der OBC wertet sie derzeit nicht aus - sie dienen nur der Pruefung der
        # Verkabelung am Bodenaufbau.
        "rexus_l0":     bool(rexus & REXUS_BIT_L0),
        "rexus_soe":    bool(rexus & REXUS_BIT_SOE),
        "rexus_sods":   bool(rexus & REXUS_BIT_SODS),
    }


# ── Registrierte Nachrichtentypen ─────────────────────────────────────────────────
register_message(SUBSYS_IMU, IMU_ACCEL, "imu", "accel", "imu", _decode_imu_accel)
register_message(SUBSYS_IMU, IMU_GYRO,  "imu", "gyro",  "imu", _decode_imu_gyro)
register_message(SUBSYS_MOTOR, MOTOR_STATE, "motor", "state", "motor", _decode_motor)
register_message(SUBSYS_FORCE, FORCE_TARGET1, "force", "target1", "force", _decode_force)
register_message(SUBSYS_FORCE, FORCE_TARGET2, "force", "target2", "force2", _decode_force2)
def _decode_sys_uplink(data: bytes, status2: int) -> dict:
    # DATA: [rx_bytes(uint16 BE) frames_ok(uint16 BE) frames_bad(uint16 BE)
    #        last_opcode(uint8) spare]
    rx_bytes, frames_ok, frames_bad = struct.unpack_from(">HHH", data)
    last_opcode = data[6] if len(data) > 6 else 0xFF
    return {
        # rx_bytes zaehlt JEDES Byte am RX-Pin, auch die RXSM-Huelle vor dem
        # eigentlichen Command-Frame. Bleibt der Wert beim Senden eines
        # Telecommands stehen, erreicht es die UART gar nicht erst.
        "uplink_rx_bytes":    rx_bytes,
        "uplink_frames_ok":   frames_ok,
        "uplink_frames_bad":  frames_bad,
        "uplink_last_opcode": None if last_opcode == 0xFF else last_opcode,
    }


def _decode_sys_uplink_raw(data: bytes, status2: int) -> dict:
    # DATA: die ersten 8 Bytes des letzten Uplink-Bursts, roh wie empfangen.
    # Die gueltige Laenge steht in STATUS2 (der OBC fuellt nur so viele Bytes,
    # wie er empfangen hat). Kam ein kurzer Burst, sind die restlichen Bytes
    # Fuellnullen und wuerden die Kopfpruefung unten sonst faelschlich
    # durchfallen lassen.
    length = min(status2, 8) if status2 else 8
    raw = list(data[:length])

    # Diagnose 1: passt der Kopf zum erwarteten SDC-Paket?
    head_ok = all(exp is None or got == exp
                  for got, exp in zip(raw, SDC_EXPECTED_HEAD))

    # Diagnose 2: sind die Bytes bitweise invertiert? Klassisch bei einer
    # RS-232-Strecke, deren Invertierung in einer Richtung nicht aufgehoben wird.
    inverted = [b ^ 0xFF for b in raw]
    inverted_ok = all(exp is None or got == exp
                      for got, exp in zip(inverted, SDC_EXPECTED_HEAD))

    return {
        "uplink_raw_hex":       " ".join(f"{b:02X}" for b in raw),
        "uplink_raw_len":       length,
        "uplink_raw_head_ok":   head_ok,
        "uplink_raw_inverted":  inverted_ok,
        "uplink_raw_inv_hex":   " ".join(f"{b:02X}" for b in inverted),
    }


register_message(SUBSYS_SYS, SYS_STATE, "sys", "state", "obc", _decode_sys)
register_message(SUBSYS_SYS, SYS_UPLINK, "sys", "uplink", "obc", _decode_sys_uplink)
register_message(SUBSYS_SYS, SYS_UPLINK_RAW, "sys", "uplink_raw", "obc",
                 _decode_sys_uplink_raw)

# NEUEN WERT HINZUFÜGEN — dreimal etwas, sonst nichts. Kraftsensor 1 oben ist
# das gelebte Beispiel:
#
#   1) MSGID-Konstanten ergänzen, passend zur OBC-Firmware:
#        SUBSYS_FORCE = 0x04 / FORCE_TARGET1 = 0x01
#   2) Decoder schreiben (DATA-Bytes → skalierte Felder):  _decode_force()
#   3) Registrieren:  register_message(SUBSYS_FORCE, FORCE_TARGET1, ...)
#
# Danach wird der Typ automatisch decodiert, ins genannte Measurement
# geschrieben und im Downlink-Monitor angezeigt — ohne weitere Codeänderung.
# Für Kraftsensor 2 (Target 2, Pin 30/31/32/41) genügt später FORCE_TARGET2 =
# 0x02 plus ein register_message() auf denselben Decoder.


def _decode_fields(msgid1: int, msgid2: int, data: bytes, status2: int) -> dict:
    """Decodiert die 8 DATA-Bytes über die Registry (unbekannt → Rohbytes)."""
    mt = MESSAGE_TYPES.get((msgid1, msgid2))
    if mt is None:
        return {"raw_hex": data.hex()}
    return mt.decoder(data, status2)


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
        fields=_decode_fields(msgid1, msgid2, data, status2),
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

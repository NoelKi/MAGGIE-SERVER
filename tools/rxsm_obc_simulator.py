#!/usr/bin/env python3
"""
MAGGIE – OBC-Simulator für den seriellen RXSM-Pfad
==================================================

Spielt den OBC (Teensy 4.1, Serial4) auf der anderen Seite des RXSM nach:
sendet den 20-Byte-Downlink (IMU, Motor, Systemzustand) und führt die
6-Byte-Telecommands aus, die der Server über den TC-Port schickt.

Damit lässt sich die komplette Kette GS → Server → (RXSM) → OBC → Server → GS
ohne Hardware durchspielen — inklusive Test-Zustand und HDRM-Fahrt.

Ground Truth der Protokolle:
  MAGGIE_OBC/include/hal/telemetry_hal.hpp   (Downlink, 20 Bytes)
  MAGGIE_OBC/include/hal/uplink_hal.hpp      (Uplink, 6 Bytes)
  MAGGIE_OBC/include/statemachine/mission_state.hpp

Aufruf ohne Argumente: der Simulator legt zwei PTY-Paare an und gibt die
Portnamen aus, die in die .env des Servers gehören:

    python tools/rxsm_obc_simulator.py

    SERIAL_PORT=/dev/ttys012        # Downlink: Server liest
    TC_SERIAL_PORT=/dev/ttys014     # Uplink:   Server schreibt

Mit echter Hardware (z.B. zwei USB-Seriell-Adapter):

    python tools/rxsm_obc_simulator.py --downlink /dev/tty.usbserial-A --uplink /dev/tty.usbserial-B
"""

from __future__ import annotations

import os
import sys
import math
import time
import errno
import struct
import argparse

# ── Downlink (telemetry_hal.hpp) ────────────────────────────────────────────
DL_START, DL_END = 0x7E, 0x7F
SUBSYS_IMU, SUBSYS_MOTOR, SUBSYS_SYS, SUBSYS_FORCE = 0x01, 0x02, 0x03, 0x04
IMU_ACCEL, IMU_GYRO, MOTOR_STATE, SYS_STATE = 0x01, 0x02, 0x01, 0x01
FORCE_TARGET1, FORCE_TARGET2 = 0x01, 0x02

STATUS1_SYSTEM_HEALTHY, STATUS1_IMU_VALID = 0x01, 0x02
MOTOR_ON, MOTOR_ENCODER_OK, MOTOR_TURNING = 0x01, 0x02, 0x04
MOTOR_TURN_FAILED = 0x08
# Ein Saettigungsbit je Kanal, Flags stehen in STATUS2 (nicht in DATA).
FORCE_SAT = (0x01, 0x02, 0x04, 0x08)
FORCE_TARED, FORCE_STALE = 0x10, 0x20
# IMU + Motor + Downlink + Kraftsensor 1 + Kraftsensor 2 bereit
SUBSYS_BITS_ALL = 0x01 | 0x02 | 0x04 | 0x08 | 0x10

# ── Uplink (uplink_hal.hpp) ─────────────────────────────────────────────────
UL_START, UL_END = 0x7E, 0x7F
UPLINK_FRAME_SIZE = 6
# 0x02..0x04 sind stillgelegt (Positionsregelung entfallen) und werden hier
# bewusst nicht mehr behandelt — sie laufen in den Unbekannt-Zweig.
OP_MOTOR_OFF, OP_MOTOR_ON, OP_MOTOR_ZERO = 0x00, 0x01, 0x05
OP_MOTOR_TURN, OP_MOTOR_GOTO = 0x06, 0x07
OP_TEST_ENTER, OP_TEST_EXIT, OP_ABORT = 0x10, 0x11, 0x1F
OP_FORCE_TARE = 0x20

# ── Motor (motor_hal.hpp) ───────────────────────────────────────────────────
COUNTS_PER_REV = 4550
DEFAULT_ON_SPEED = 220       # OBC-Default, wenn ARG 0 ist
TURN_SPEED = 220             # feste Geschwindigkeit fuer MOTOR_TURN/GOTO
MIN_DRIVE_SPEED = 220        # darunter laeuft der Motor nicht an

# Abbruchkriterium einer Drehung (motor_hal.hpp: TURN_STALL_MS/TURN_MIN_COUNTS)
TURN_STALL_S = 1.0
TURN_MIN_COUNTS = 3

# Zielfenster fuer MOTOR_GOTO (motor_hal.hpp: POS_DEADBAND)
POS_DEADBAND = 200
# Grobe Leerlaufdrehzahl des Prototyps bei voller PWM. Die simulierte Position
# laeuft proportional zum PWM-Stellwert, damit sich in der Bodenstation auch
# Richtung und Geschwindigkeit unterscheiden lassen.
COUNTS_PER_S_AT_FULL = 1500.0 * 255.0 / 120.0

# ── Zustände (mission_state.hpp) ────────────────────────────────────────────
# 1..4 sind für die frühere Flugsequenz reserviert und werden nicht gesendet.
PRE_LAUNCH, ABORT, TEST = 0, 5, 6
STATE_NAMES = {PRE_LAUNCH: "PRE_LAUNCH", ABORT: "ABORT", TEST: "TEST"}

TELEMETRY_HZ = 20
SYS_HZ = 1
# Der HX711 wandelt mit 10 Hz — der OBC sendet im Takt des Sensors, nicht im
# Telemetrie-Intervall. Hier genauso, damit die Frame-Raten am Boden stimmen.
FORCE_HZ = 10

# ── Kraftsensoren (force_hal.hpp) ───────────────────────────────────────────
# Elektrische Nullpunkte der Wandler im UNBELASTETEN Zustand, in der
# Groessenordnung eines echten HX711 (zehntausende Counts). Sie sprengen das
# int16 des Frames und sind der Grund, warum der OBC tarieren muss.
FORCE1_ZERO = (120_000, -95_000, 210_000)           # X, Y, Z
FORCE2_ZERO = (130_000, -80_000, 175_000, 96_000)   # A, B, C, D

# Simulierte Querlast auf den drei 120-Grad-Zellen von Sensor 2.
FORCE2_LOAD_COUNTS    = 6_000.0   # Betrag der umlaufenden Querlast
FORCE2_PRELOAD_COUNTS = 2_500.0   # Gleichanteil, muss am Boden herausfallen
FORCE2_SPIN_DEG_PER_S = 30.0      # Umlaufgeschwindigkeit der Lastrichtung


def crc8(data: bytes) -> int:
    """CRC-8/SMBUS (poly 0x07, init 0x00) — identisch zur OBC-Firmware."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class ObcSimulator:
    """Motor-, Zustands- und Telemetriemodell des OBC."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.counter = 0
        self.state = PRE_LAUNCH

        # Motor (Open Loop): dreht mit self.pwm, bis MOTOR_OFF kommt oder eine
        # MOTOR_TURN-Drehung ihr Encoder-Ziel erreicht.
        self.position = 0.0
        self.on = False
        self.pwm = 0
        self.turn_target: float | None = None
        self.turn_ref_pos = 0.0
        self.turn_ref_t = 0.0
        self.turn_failed = False

        # None | "dead" | "inverted" — simulierter Encoder-Fehler, siehe step().
        self.encoder_fault: str | None = None

        # Kraftsensoren: HX711-Rohsignale mit grossem Nullpunkt-Offset, wie ihn
        # ein echter Wandler hat. Der Offset ist genau der Grund, warum der OBC
        # tarieren MUSS, bevor die Werte ins int16 des Frames passen.
        #
        # Sensor 2 simuliert eine UMLAUFENDE Querlast auf den drei 120-Grad-
        # Zellen plus eine langsame Z-Last auf der vierten. Damit laesst sich am
        # Boden pruefen, ob die Clarke-Transformation richtig herum rechnet:
        # force2_dir_deg muss gleichmaessig durchlaufen, |xy| konstant bleiben.
        # Nullabgleich beim Start wie ForceHAL::init() im OBC — und zwar gegen
        # den UNBELASTETEN Zustand, also die reinen elektrischen Offsets. Wuerde
        # hier gegen den momentanen Messwert tariert, subtrahierte der Sensor
        # eine echte Last und alle spaeteren Werte waeren dagegen gemessen.
        # (Genau das passiert am Aufbau, wenn beim Booten etwas aufliegt.)
        self.force1_offset = FORCE1_ZERO
        self.force2_offset = FORCE2_ZERO
        self.force1_tared = True
        self.force2_tared = True

        # Nachlauf in Counts, den der Motor nach dem Abschalten noch macht.
        # Richtungsabhaengig, weil Federkraft, Schwerkraft und Reibung am HDRM
        # in beiden Richtungen unterschiedlich wirken — genau diese Asymmetrie
        # laesst die Nullage bei RELATIVEN Fahrten wandern.
        self.overshoot_fwd = 0.0
        self.overshoot_rev = 0.0

        self._rx = bytearray()

    # -- Zustand ------------------------------------------------------------
    @property
    def actuators_unlocked(self) -> bool:
        return self.state == TEST

    def _set_state(self, new: int) -> None:
        if new == self.state:
            return
        print(f"[OBC] Zustand {STATE_NAMES[self.state]} -> {STATE_NAMES[new]}")
        self.state = new
        if not self.actuators_unlocked:
            self.on = False
            self.pwm = 0
            self.turn_target = None

    # -- Telecommands -------------------------------------------------------
    def feed_uplink(self, data: bytes) -> None:
        """Byte-Strom des RXSM einlesen und gültige Kommandos ausführen."""
        self._rx.extend(data)
        while True:
            start = self._rx.find(UL_START)
            if start < 0:
                self._rx.clear()
                return
            if len(self._rx) - start < UPLINK_FRAME_SIZE:
                del self._rx[:start]
                return
            frame = bytes(self._rx[start:start + UPLINK_FRAME_SIZE])
            if frame[5] == UL_END and crc8(frame[1:4]) == frame[4]:
                self._exec(frame[1], struct.unpack(">h", frame[2:4])[0])
                del self._rx[:start + UPLINK_FRAME_SIZE]
            else:
                del self._rx[:start + 1]      # Fehlstart -> ein Byte weiter

    def _exec(self, opcode: int, arg: int) -> None:
        if opcode == OP_TEST_ENTER:
            if self.state == PRE_LAUNCH:
                self._set_state(TEST)
            else:
                print(f"[OBC] TEST_ENTER abgelehnt — Zustand {STATE_NAMES[self.state]}")
            return
        if opcode == OP_TEST_EXIT:
            if self.state == TEST:
                self._set_state(PRE_LAUNCH)
            return
        if opcode == OP_ABORT:
            self._set_state(ABORT)
            return
        if opcode == OP_MOTOR_OFF:
            # Sicherheitskommando: zustandsunabhängig erlaubt (wie im OBC).
            self.on, self.pwm = False, 0
            self.turn_target = None
            print("[OBC] TC MOTOR_OFF (zustandsunabhaengig)")
            return
        if opcode == OP_FORCE_TARE:
            # Kein Aktor -> zustandsunabhängig, wie im OBC.
            # ARG: 0 = beide, 1 = Kraftsensor 1, 2 = Kraftsensor 2.
            if arg not in (0, 1, 2):
                print(f"[OBC] FORCE_TARE mit ungueltigem ARG {arg} - ignoriert")
                return
            self.tare_force(arg)
            which = {0: "beide Kraftsensoren", 1: "Kraftsensor 1",
                     2: "Kraftsensor 2"}[arg]
            print(f"[OBC] TC FORCE_TARE - {which} genullt")
            return

        if not self.actuators_unlocked:
            print(f"[OBC] TC 0x{opcode:02X} abgewiesen — Aktoren gesperrt "
                  f"(Zustand {STATE_NAMES[self.state]})")
            return

        if opcode == OP_MOTOR_ON:
            speed = arg if arg != 0 else DEFAULT_ON_SPEED
            speed = max(-255, min(255, speed))
            # Anlaufgrenze wie MotorHAL::setSpeed()
            if 0 < speed < MIN_DRIVE_SPEED:
                speed = MIN_DRIVE_SPEED
            elif -MIN_DRIVE_SPEED < speed < 0:
                speed = -MIN_DRIVE_SPEED
            self.on, self.pwm = True, speed
            self.turn_target = None
            print(f"[OBC] TC MOTOR_ON (speed={self.pwm})")
        elif opcode == OP_MOTOR_ZERO:
            self.position, self.on, self.pwm = 0.0, False, 0
            self.turn_target = None
            print("[OBC] TC MOTOR_ZERO — Encoder-Zaehler auf 0")
        elif opcode == OP_MOTOR_TURN:
            delta = int(arg) * COUNTS_PER_REV // 360
            if delta == 0:
                print(f"[OBC] TC MOTOR_TURN ({arg} Grad) — zu klein, ignoriert")
                return
            print(f"[OBC] TC MOTOR_TURN ({arg} Grad relativ = {delta} Counts)")
            self._start_move(self.position + delta)
        elif opcode == OP_MOTOR_GOTO:
            target = int(arg) * COUNTS_PER_REV // 360
            if abs(target - self.position) <= POS_DEADBAND:
                print(f"[OBC] TC MOTOR_GOTO ({arg} Grad) — Position "
                      f"{self.position:.0f} schon im Zielfenster, keine Fahrt")
                return
            print(f"[OBC] TC MOTOR_GOTO ({arg} Grad absolut = {target} Counts)")
            self._start_move(target)
        else:
            print(f"[OBC] Unbekanntes TC 0x{opcode:02X} (arg {arg}) ignoriert")

    def _start_move(self, target: float) -> None:
        self.on = True
        self.pwm = TURN_SPEED if target > self.position else -TURN_SPEED
        self.turn_target = target
        self.turn_ref_pos = self.position
        self.turn_ref_t = 0.0
        self.turn_failed = False
        print(f"[OBC]   Start {self.position:.0f} -> Ziel {target:.0f}")

    # -- Motormodell --------------------------------------------------------
    def step(self, dt: float) -> None:
        """Open Loop: die Position laeuft proportional zur PWM, sonst steht sie."""
        if not (self.on and self.pwm):
            self.pwm = 0
            return

        # encoder_fault stellt die beiden Ausfaelle nach, bei denen eine Drehung
        # ihr Ziel nie erreicht: "dead" = Encoder zaehlt nicht, "inverted" =
        # Kanaele A/B vertauscht, die Position laeuft vom Ziel weg.
        rate = COUNTS_PER_S_AT_FULL * (self.pwm / 255.0)
        if self.encoder_fault == "dead":
            rate = 0.0
        elif self.encoder_fault == "inverted":
            rate = -rate
        self.position += rate * dt

        if self.turn_target is None:
            return

        # Endschalter wie updateTurn() im OBC: Vergleich in Fahrtrichtung.
        reached = (self.position >= self.turn_target if self.pwm > 0
                   else self.position <= self.turn_target)
        if reached:
            # Nachlauf: der Motor steht nicht schlagartig, sondern laeuft noch
            # ein Stueck weiter (bzw. bremst kuerzer, wenn gebremst wird).
            self.position += (self.overshoot_fwd if self.pwm > 0
                              else -self.overshoot_rev)
            print(f"[OBC] Fahrt beendet — Position {self.position:.0f} "
                  f"(Ziel {self.turn_target:.0f})")
            self.turn_target = None
            self.on, self.pwm = False, 0
            return

        # Fortschrittsfenster wie TURN_STALL_MS/TURN_MIN_COUNTS im OBC.
        progress = (self.position - self.turn_ref_pos if self.pwm > 0
                    else self.turn_ref_pos - self.position)
        if progress >= TURN_MIN_COUNTS:
            self.turn_ref_pos = self.position
            self.turn_ref_t = 0.0
        else:
            self.turn_ref_t += dt
            if self.turn_ref_t >= TURN_STALL_S:
                print(f"[OBC] Drehung abgebrochen — in {TURN_STALL_S:.0f} s nur "
                      f"{progress:.0f} Counts in Fahrtrichtung "
                      f"(Position {self.position:.0f}, Ziel {self.turn_target:.0f})")
                self.turn_target = None
                self.turn_failed = True
                self.on, self.pwm = False, 0

    # -- Downlink -----------------------------------------------------------
    def _frame(self, msgid1: int, msgid2: int, data: bytes, status1: int,
               status2: int = 0) -> bytes:
        self.counter = (self.counter + 1) & 0xFFFF
        t = int((time.time() - self.t0) * 1000) & 0xFFFF
        body = bytes([msgid1, msgid2, 0]) + struct.pack(">HH", self.counter, t) \
            + data + bytes([status1, status2])
        return bytes([DL_START]) + body + bytes([crc8(body), DL_END])

    def imu_frames(self) -> bytes:
        """Ruhende IMU: 1 g auf z, etwas Rauschen (BMI088-Counts)."""
        t = time.time() - self.t0
        n = lambda f, a: int(a * math.sin(t * f))                      # noqa: E731
        accel = struct.pack(">hhh", n(3.1, 60), n(2.3, 45), 10920 + n(5.0, 30))
        gyro  = struct.pack(">hhh", n(1.7, 90), n(2.9, 70), n(4.1, 50))
        s1 = STATUS1_SYSTEM_HEALTHY | STATUS1_IMU_VALID
        return (self._frame(SUBSYS_IMU, IMU_ACCEL, accel + bytes(2), s1)
                + self._frame(SUBSYS_IMU, IMU_GYRO, gyro + bytes(2), s1))

    def _force1_now(self) -> tuple[int, ...]:
        """Rohe HX711-Counts von Sensor 1 (X/Y/Z) inkl. Nullpunkt-Offset."""
        t = time.time() - self.t0
        return tuple(
            int(zero + amp * math.sin(t * f + p))
            for zero, amp, f, p in zip(FORCE1_ZERO, (8_000, 6_000, 4_000),
                                       (0.7, 0.5, 0.9), (0.0, 1.0, 2.5))
        )

    def _force2_now(self) -> tuple[int, ...]:
        """
        Rohe HX711-Counts von Sensor 2 (A/B/C/D) inkl. Nullpunkt-Offset.

        Modelliert eine Querlast KONSTANTEN Betrags, die um die Achse wandert:
        Jede der drei 120-Grad-Zellen misst die Projektion cos(phi - theta_i).
        Dazu ein Gleichanteil (Montagevorspannung), der in der Bodenrechnung
        herausfallen MUSS - genau das macht ihn zum nuetzlichen Testfall.

        Am Boden muss dabei herauskommen: force2_xy_mag konstant,
        force2_dir_deg gleichmaessig mit FORCE2_SPIN_DEG_PER_S umlaufend.
        Stimmt der Winkel nicht, ist FORCE2_CELL_ANGLES_DEG im Parser falsch
        oder die Zellen sind vertauscht verkabelt.
        """
        t = time.time() - self.t0
        phi = t * FORCE2_SPIN_DEG_PER_S
        radial = [
            FORCE2_LOAD_COUNTS * math.cos(math.radians(phi - theta))
            + FORCE2_PRELOAD_COUNTS
            for theta in (0.0, 120.0, 240.0)
        ]
        z = 4_000.0 * math.sin(t * 0.4)      # langsame Z-Last auf Zelle D
        return tuple(int(o + v) for o, v in zip(FORCE2_ZERO, radial + [z]))

    def _force_frame(self, msgid2: int, raw: tuple[int, ...],
                     offset: tuple[int, ...], tared: bool) -> bytes:
        """
        Ein FORCE-Frame bauen — tarierte Counts, saturiert auf int16.

        Spiegelt ForceHAL + TelemetryDownlink::sendForce: Die Flags gehen in
        STATUS2, nicht ins DATA-Feld (TARGET2 braucht alle acht Datenbytes).
        """
        flags = FORCE_TARED if tared else 0
        values = []
        for i, (r, o) in enumerate(zip(raw, offset)):
            value = r - o
            if value > 32767:
                value = 32767
                flags |= FORCE_SAT[i]
            elif value < -32768:
                value = -32768
                flags |= FORCE_SAT[i]
            values.append(value)

        data = struct.pack(">" + "h" * len(values), *values)
        data += bytes(8 - len(data))          # nicht belegte Kanaele bleiben 0
        return self._frame(SUBSYS_FORCE, msgid2, data, STATUS1_SYSTEM_HEALTHY, flags)

    def force1_frame(self) -> bytes:
        return self._force_frame(FORCE_TARGET1, self._force1_now(),
                                 self.force1_offset, self.force1_tared)

    def force2_frame(self) -> bytes:
        return self._force_frame(FORCE_TARGET2, self._force2_now(),
                                 self.force2_offset, self.force2_tared)

    def tare_force(self, sensor: int) -> None:
        """sensor: 0 = beide, 1 = Kraftsensor 1, 2 = Kraftsensor 2."""
        if sensor in (0, 1):
            self.force1_offset = self._force1_now()
            self.force1_tared = True
        if sensor in (0, 2):
            self.force2_offset = self._force2_now()
            self.force2_tared = True

    def motor_frame(self) -> bytes:
        state = MOTOR_ENCODER_OK          # simulierter Encoder ist immer da
        if self.on:
            state |= MOTOR_ON
        if self.turn_target is not None:
            state |= MOTOR_TURNING
        if self.turn_failed:
            state |= MOTOR_TURN_FAILED
        data = struct.pack(">ihBB", int(round(self.position)), self.pwm, state, 0)
        return self._frame(SUBSYS_MOTOR, MOTOR_STATE, data, STATUS1_SYSTEM_HEALTHY)

    def sys_frame(self) -> bytes:
        uptime_ms = int((time.time() - self.t0) * 1000)
        data = struct.pack(">BBI", self.state, SUBSYS_BITS_ALL, uptime_ms) + bytes(2)
        return self._frame(SUBSYS_SYS, SYS_STATE, data, STATUS1_SYSTEM_HEALTHY)


# ── Transport: PTY-Paare oder echte serielle Ports ──────────────────────────
class PtyLink:
    """Ein PTY-Paar; der Server öffnet den Slave-Pfad, der Simulator den Master."""

    def __init__(self) -> None:
        self.master, slave = os.openpty()
        self.device = os.ttyname(slave)
        os.set_blocking(self.master, False)

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def read(self) -> bytes:
        try:
            return os.read(self.master, 4096)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            return b""


class SerialLink:
    """Echter serieller Port (pyserial)."""

    def __init__(self, port: str, baud: int) -> None:
        import serial
        self._ser = serial.Serial(port, baud, timeout=0)
        self.device = port

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def read(self) -> bytes:
        return self._ser.read(4096)


def main() -> int:
    ap = argparse.ArgumentParser(description="MAGGIE OBC-Simulator (serieller RXSM-Pfad)")
    ap.add_argument("--downlink", help="serieller Port, auf dem der Simulator sendet "
                                       "(leer = PTY anlegen)")
    ap.add_argument("--uplink", help="serieller Port, auf dem der Simulator Telecommands "
                                     "empfängt (leer = PTY anlegen)")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="nach N Sekunden beenden (0 = endlos)")
    args = ap.parse_args()

    down = SerialLink(args.downlink, args.baud) if args.downlink else PtyLink()
    up   = SerialLink(args.uplink, args.baud) if args.uplink else PtyLink()

    print("MAGGIE OBC-Simulator")
    print("  Downlink (Server liest):   SERIAL_PORT=" + down.device)
    print("  Uplink   (Server schreibt): TC_SERIAL_PORT=" + up.device)
    print(f"  Telemetrie {TELEMETRY_HZ} Hz, Kraft {FORCE_HZ} Hz, "
          f"Zustand {SYS_HZ} Hz — Strg-C beendet\n")

    obc = ObcSimulator()
    tick = 1.0 / TELEMETRY_HZ
    next_tel = next_sys = next_force = time.time()
    last = time.time()
    stop_at = time.time() + args.seconds if args.seconds else None

    try:
        while stop_at is None or time.time() < stop_at:
            now = time.time()
            obc.step(now - last)
            last = now

            data = up.read()
            if data:
                obc.feed_uplink(data)

            if now >= next_tel:
                next_tel += tick
                down.write(obc.imu_frames() + obc.motor_frame())

            if now >= next_force:
                next_force += 1.0 / FORCE_HZ
                down.write(obc.force1_frame() + obc.force2_frame())

            if now >= next_sys:
                next_sys += 1.0 / SYS_HZ
                down.write(obc.sys_frame())

            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nSimulator beendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

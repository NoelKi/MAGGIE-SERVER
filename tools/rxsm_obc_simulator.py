#!/usr/bin/env python3
"""
MAGGIE – OBC-Simulator für den seriellen RXSM-Pfad
==================================================

Spielt den OBC (Teensy 4.1, Serial8) auf der anderen Seite des RXSM nach:
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
SUBSYS_IMU, SUBSYS_MOTOR, SUBSYS_SYS = 0x01, 0x02, 0x03
IMU_ACCEL, IMU_GYRO, MOTOR_STATE, SYS_STATE = 0x01, 0x02, 0x01, 0x01

STATUS1_SYSTEM_HEALTHY, STATUS1_IMU_VALID = 0x01, 0x02
MOTOR_ON, MOTOR_MOVING, MOTOR_AT_TARGET = 0x01, 0x02, 0x04
MOTOR_HDRM_OPEN, MOTOR_HDRM_CLOSED = 0x08, 0x10
SUBSYS_BITS_ALL = 0x01 | 0x02 | 0x04            # IMU + Motor + Downlink bereit

# ── Uplink (uplink_hal.hpp) ─────────────────────────────────────────────────
UL_START, UL_END = 0x7E, 0x7F
UPLINK_FRAME_SIZE = 6
OP_MOTOR_OFF, OP_MOTOR_ON, OP_HALF_TURN_LEGACY = 0x00, 0x01, 0x02
OP_HALF_TURN_FWD, OP_HALF_TURN_REV, OP_MOTOR_ZERO = 0x03, 0x04, 0x05
OP_TEST_ENTER, OP_TEST_EXIT, OP_ABORT = 0x10, 0x11, 0x1F

# ── Motor (motor_hal.hpp) ───────────────────────────────────────────────────
COUNTS_PER_REV = 4600
HALF_TURN = COUNTS_PER_REV // 2
HDRM_TOL = 60
COUNTS_PER_S = 1500.0        # grobe Leerlaufdrehzahl des Prototyps

# ── Zustände (mission_state.hpp) ────────────────────────────────────────────
# 1..4 sind für die frühere Flugsequenz reserviert und werden nicht gesendet.
PRE_LAUNCH, ABORT, TEST = 0, 5, 6
STATE_NAMES = {PRE_LAUNCH: "PRE_LAUNCH", ABORT: "ABORT", TEST: "TEST"}

TELEMETRY_HZ = 20
SYS_HZ = 1


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

        # Motor: Encoder-Nullpunkt = "HDRM geschlossen"
        self.position = 0.0
        self.target: float | None = None
        self.on = False
        self.pwm = 0

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
            self.target = None
            self.pwm = 0

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
            self.on, self.target = False, None
            print("[OBC] TC MOTOR_OFF (zustandsunabhaengig)")
            return

        if not self.actuators_unlocked:
            print(f"[OBC] TC 0x{opcode:02X} abgewiesen — Aktoren gesperrt "
                  f"(Zustand {STATE_NAMES[self.state]})")
            return

        if opcode == OP_MOTOR_ON:
            self.on, self.target = True, None
            print("[OBC] TC MOTOR_ON")
        elif opcode in (OP_HALF_TURN_FWD, OP_HALF_TURN_LEGACY):
            self.target = self.position + HALF_TURN
            print(f"[OBC] TC HALF_TURN_FWD -> {self.target:.0f} cts")
        elif opcode == OP_HALF_TURN_REV:
            self.target = self.position - HALF_TURN
            print(f"[OBC] TC HALF_TURN_REV -> {self.target:.0f} cts")
        elif opcode == OP_MOTOR_ZERO:
            self.position, self.target, self.on = 0.0, None, False
            print("[OBC] TC MOTOR_ZERO — Nullpunkt gesetzt")
        else:
            print(f"[OBC] Unbekanntes TC 0x{opcode:02X} (arg {arg}) ignoriert")

    # -- Motormodell --------------------------------------------------------
    def step(self, dt: float) -> None:
        if self.target is not None:
            error = self.target - self.position
            if abs(error) <= 5:
                self.position, self.target, self.pwm = self.target, None, 0
            else:
                step = min(abs(error), COUNTS_PER_S * dt)
                self.position += math.copysign(step, error)
                self.pwm = 60 if error > 0 else -60
        elif self.on:
            self.position += COUNTS_PER_S * dt
            self.pwm = 120
        else:
            self.pwm = 0

    # -- Downlink -----------------------------------------------------------
    def _frame(self, msgid1: int, msgid2: int, data: bytes, status1: int) -> bytes:
        self.counter = (self.counter + 1) & 0xFFFF
        t = int((time.time() - self.t0) * 1000) & 0xFFFF
        body = bytes([msgid1, msgid2, 0]) + struct.pack(">HH", self.counter, t) \
            + data + bytes([status1, 0])
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

    def motor_frame(self) -> bytes:
        state = MOTOR_ON if self.on else 0
        state |= MOTOR_MOVING if self.target is not None else MOTOR_AT_TARGET
        pos = int(round(self.position))
        if abs(pos - HALF_TURN) <= HDRM_TOL:
            state |= MOTOR_HDRM_OPEN
        if abs(pos) <= HDRM_TOL:
            state |= MOTOR_HDRM_CLOSED
        data = struct.pack(">ihBB", pos, self.pwm, state, 0)
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
    print(f"  Telemetrie {TELEMETRY_HZ} Hz, Zustand {SYS_HZ} Hz — Strg-C beendet\n")

    obc = ObcSimulator()
    tick = 1.0 / TELEMETRY_HZ
    next_tel = next_sys = time.time()
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

            if now >= next_sys:
                next_sys += 1.0 / SYS_HZ
                down.write(obc.sys_frame())

            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nSimulator beendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

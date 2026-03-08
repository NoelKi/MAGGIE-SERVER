#!/usr/bin/env python3
"""
MAGGIE – OBC Simulator
======================

Simuliert den REXUS On-Board-Computer: sendet UDP-Pakete im MAGGIE-Protokoll
an den Ground-Station-Server.

Verwendung:
    python3 tools/obc_simulator.py                  # 1 kompletter Flug
    python3 tools/obc_simulator.py --host 192.168.1.10 --port 9000
    python3 tools/obc_simulator.py --phase ascent --count 20

Argumente:
    --host    IP des Ground-Station-Servers  (default: 127.0.0.1)
    --port    UDP-Port                       (default: 9000)
    --phase   Nur diese Phase simulieren     (default: full_flight)
    --count   Pakete pro Measurement-Type   (default: 10)
    --rate    Pakete/Sekunde                 (default: 10)
"""

import argparse
import math
import random
import socket
import sys
import time

# Pfad für lokale Imports setzen
sys.path.insert(0, __file__.rsplit("/tools", 1)[0])

from app.services.packet_parser import (
    build_packet,
    TYPE_IMU,
    TYPE_ENVIRONMENT,
    TYPE_GPS,
    TYPE_SYSTEM,
    TYPE_HEARTBEAT,
    FLAG_LIFTOFF,
    FLAG_SODS,
    FLAG_SOEX,
    FLAG_BURNOUT,
    FLAG_APOGEE,
    FLAG_PARACHUTE,
)


def send_packet(sock: socket.socket, host: str, port: int, raw: bytes) -> None:
    sock.sendto(raw, (host, port))


def simulate_flight(host: str, port: int, count: int, rate: float) -> None:
    """Simuliert einen kompletten REXUS-Flug (~120 s)."""
    delay = 1.0 / rate
    seq   = 0
    t_ms  = 0   # ms seit Lift-off

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        print(f"🚀  OBC-Simulator → {host}:{port}  ({rate} Pkts/s)")

        # ── Phase 1: Boden (vor Lift-off) ─────────────────────────────
        print("⏳  Boden-Phase (Heartbeat × 3)...")
        for i in range(3):
            pkt = build_packet(TYPE_HEARTBEAT, seq, t_ms, 0x00,
                               {"boot_count": 1})
            send_packet(sock, host, port, pkt)
            seq += 1; t_ms += 1000
            time.sleep(1.0)

        # ── Phase 2: Lift-off & Aufstieg ──────────────────────────────
        print("🔺  Aufstieg (Lift-off + Burnout)...")
        flags = FLAG_LIFTOFF | FLAG_SODS | FLAG_SOEX
        for i in range(count):
            alt   = i * 80.0 + random.uniform(-5, 5)          # 0–800 m
            ax    = 25.0 + random.uniform(-2, 2)               # Beschleunigung ~25 m/s²
            spin  = 4.0 * math.pi + random.uniform(-0.1, 0.1) # ~4 Hz

            pkt = build_packet(TYPE_IMU, seq, t_ms, flags, {
                "ax": ax, "ay": random.uniform(-1, 1), "az": random.uniform(-1, 1),
                "gx": spin, "gy": random.uniform(-0.5, 0.5), "gz": random.uniform(-0.5, 0.5),
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += int(1000 / rate)

            pkt = build_packet(TYPE_ENVIRONMENT, seq, t_ms, flags, {
                "temperature": 15.0 - alt * 0.006,
                "pressure":    1013.0 - alt * 0.12,
                "humidity":    40.0 + random.uniform(-5, 5),
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += int(1000 / rate)

            pkt = build_packet(TYPE_GPS, seq, t_ms, flags, {
                "latitude":  68.425 + i * 0.0001,
                "longitude": 21.075 + i * 0.00005,
                "altitude":  alt,
                "speed":     i * 8.0 + random.uniform(-2, 2),
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += int(1000 / rate)

            pkt = build_packet(TYPE_SYSTEM, seq, t_ms, flags, {
                "cpu_temp":        35.0 + random.uniform(-2, 2),
                "battery_voltage": 3.85 - i * 0.002,
                "battery_current": 150.0 + random.uniform(-10, 10),
                "uptime_s":        t_ms / 1000.0,
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += int(1000 / rate)
            time.sleep(delay * 4)

        # ── Phase 3: Brennschluss & Küstenflug ────────────────────────
        print("💨  Küstenflug (Motor aus)...")
        flags = FLAG_LIFTOFF | FLAG_SODS | FLAG_SOEX | FLAG_BURNOUT
        for i in range(count // 2):
            alt = count * 80.0 + i * 200.0
            pkt = build_packet(TYPE_IMU, seq, t_ms, flags, {
                "ax": random.uniform(-0.5, 0.5),   # fast schwerelos
                "ay": random.uniform(-0.5, 0.5),
                "az": -9.81 + random.uniform(-0.2, 0.2),  # nur Gravitation
                "gx": 0.5 + random.uniform(-0.1, 0.1),
                "gy": random.uniform(-0.1, 0.1),
                "gz": random.uniform(-0.1, 0.1),
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += 500
            time.sleep(delay)

        # ── Phase 4: Apogee ────────────────────────────────────────────
        print("🎯  Apogee erreicht!")
        flags = FLAG_LIFTOFF | FLAG_SODS | FLAG_SOEX | FLAG_BURNOUT | FLAG_APOGEE
        for i in range(5):
            pkt = build_packet(TYPE_GPS, seq, t_ms, flags, {
                "latitude":  68.435,
                "longitude": 21.082,
                "altitude":  80000.0 + random.uniform(-100, 100),  # ~80 km
                "speed":     random.uniform(-5, 5),
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += 500
            time.sleep(0.5)

        # ── Phase 5: Abstieg & Fallschirm ─────────────────────────────
        print("🪂  Abstieg mit Fallschirm...")
        flags = FLAG_LIFTOFF | FLAG_SODS | FLAG_SOEX | FLAG_BURNOUT | FLAG_APOGEE | FLAG_PARACHUTE
        for i in range(count // 2):
            alt = 80000.0 - i * 1500.0
            pkt = build_packet(TYPE_ENVIRONMENT, seq, t_ms, flags, {
                "temperature": -50.0 + i * 1.5,
                "pressure":    10.0  + i * 15.0,
                "humidity":    60.0 + random.uniform(-5, 5),
            })
            send_packet(sock, host, port, pkt); seq += 1; t_ms += 2000
            time.sleep(delay)

        print(f"✅  Simulation fertig — {seq} Pakete gesendet")


def main() -> None:
    parser = argparse.ArgumentParser(description="MAGGIE OBC Simulator")
    parser.add_argument("--host",  default="127.0.0.1", help="Ground-Station IP")
    parser.add_argument("--port",  default=9000, type=int, help="UDP-Port")
    parser.add_argument("--count", default=10,   type=int, help="Pakete pro Typ")
    parser.add_argument("--rate",  default=10.0, type=float, help="Pakete/Sekunde")
    args = parser.parse_args()

    simulate_flight(args.host, args.port, args.count, args.rate)


if __name__ == "__main__":
    main()

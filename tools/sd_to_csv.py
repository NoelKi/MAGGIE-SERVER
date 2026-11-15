#!/usr/bin/env python3
"""
MAGGIE – SD-Karte des OBC nach CSV
==================================

Liest die Bordspeicher-Dateien des OBC (/Bxxxxx/Fxxxxx.BIN, siehe
MAGGIE_OBC/include/storage/sd_logger.hpp) und schreibt je Messwert-Typ eine
CSV-Datei mit durchgehender Zeitachse, Nullpunkt = SODS.

    python tools/sd_to_csv.py /media/SD             # ganze Karte, alle Boots
    python tools/sd_to_csv.py /media/SD/B00042      # nur ein Boot
    python tools/sd_to_csv.py /media/SD -o flug1    # Zielordner (Default: ./sd_csv)

Ausgabe (je eine Datei, nur wenn Daten vorhanden):
    imu_accel.csv  imu_gyro.csv  motor1.csv  motor2.csv  force1.csv  force2.csv
    sys.csv  uplink.csv  uplink_raw.csv
    events.csv     Boot-Starts, Zustandswechsel, SODS/LO/SOE, verlorene Frames

Spalten in jeder Datei:
    time_s     Sekunden ab SODS (negativ = davor)
    boot       Boot-Nummer (Verzeichnis)
    t_boot_ms  OBC-Zeit seit diesem Boot (millis(), aus dem 16-Bit-TIME entfaltet)
    counter    Frame-Zaehler des OBC
    ...        alle decodierten Felder - identisch zum Live-Downlink, denn die
               Decodierung kommt aus app/services/downlink_frame_parser.py

Genauigkeit:
    - Sensorwerte: Millisekunden (TIME-Feld jedes Frames).
    - Zustand und SODS/LO/SOE: Millisekunden. Der OBC schreibt bei jeder
      Aenderung ein EVENT-Frame (MSGID 0x10, nur SD-Karte, nie im Downlink).
      Fehlen diese (aeltere Firmware), kommen die Ereignisse aus dem 1-Hz-SYS-
      Frame und sind - wie dann auch der Nullpunkt - nur auf +-1 s genau.
    - Ueber einen Neustart hinweg ist die Ausfallzeit unbekannt. Der naechste
      Boot wird direkt an das Ende des vorherigen gehaengt (plus seine eigene
      Zeit seit dem Reset). Die Spalte "boot" zeigt, wo das passiert.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Parser direkt aus app/services laden - ohne das Flask-Paket "app" zu starten.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "services"))
import downlink_frame_parser as dl  # noqa: E402

BOOT_DIR_RE = re.compile(r"^B(\d{5})(?:_(\d))?$", re.IGNORECASE)
FILE_RE     = re.compile(r"^F(\d{5})\.BIN$", re.IGNORECASE)

# Dateiname je (MSGID1, MSGID2). Unbekannte Typen landen in unknown_XX_YY.csv.
OUTPUT_NAMES = {
    (dl.SUBSYS_IMU,   dl.IMU_ACCEL):      "imu_accel",
    (dl.SUBSYS_IMU,   dl.IMU_GYRO):       "imu_gyro",
    (dl.SUBSYS_MOTOR, dl.MOTOR_STATE):    "motor1",
    (dl.SUBSYS_MOTOR, dl.MOTOR_STATE2):   "motor2",
    (dl.SUBSYS_FORCE, dl.FORCE_TARGET1):  "force1",
    (dl.SUBSYS_FORCE, dl.FORCE_TARGET2):  "force2",
    (dl.SUBSYS_SYS,   dl.SYS_STATE):      "sys",
    (dl.SUBSYS_SYS,   dl.SYS_UPLINK):     "uplink",
    (dl.SUBSYS_SYS,   dl.SYS_UPLINK_RAW): "uplink_raw",
}

BASE_COLUMNS = ["time_s", "boot", "t_boot_ms", "counter", "status1", "status2"]

# EVENT/STATE - nur auf der SD-Karte (OBC: TelemetryDownlink::logEvent).
# DATA: [state prev_state signals prev_signals millis(uint32 BE)],
# signals: Bit 0 LO, Bit 1 SOE, Bit 2 SODS, 1 = Signal liegt an (entprellt).
SUBSYS_EVENT = 0x10
EVENT_STATE  = 0x01
SIGNALS = (("LO", 0x01), ("SOE", 0x02), ("SODS", 0x04))


def _decode_event(data: bytes, status2: int) -> dict:
    state, prev_state, signals, prev_signals = data[0], data[1], data[2], data[3]
    return {
        "state_name":      dl.MISSION_STATES.get(state, f"UNKNOWN_{state}"),
        "prev_state_name": dl.MISSION_STATES.get(prev_state, f"UNKNOWN_{prev_state}"),
        "signals":         signals,
        "prev_signals":    prev_signals,
        "millis":          int.from_bytes(data[4:8], "big"),
    }


dl.register_message(SUBSYS_EVENT, EVENT_STATE, "event", "state", "event", _decode_event)


def is_event(f: dl.DownlinkFrame) -> bool:
    return (f.msgid1, f.msgid2) == (SUBSYS_EVENT, EVENT_STATE)


# ── Einlesen ──────────────────────────────────────────────────────────────────────
def find_boot_dirs(root: Path) -> list[tuple[int, int, Path]]:
    """(Boot-Nummer, Suffix, Pfad), sortiert. root darf selbst ein Boot-Ordner sein."""
    m = BOOT_DIR_RE.match(root.name)
    if m:
        return [(int(m.group(1)), int(m.group(2) or 0), root)]
    boots = []
    for p in root.iterdir():
        m = BOOT_DIR_RE.match(p.name)
        if p.is_dir() and m:
            boots.append((int(m.group(1)), int(m.group(2) or 0), p))
    return sorted(boots)


def read_boot(path: Path) -> tuple[list[dl.DownlinkFrame], int]:
    """Alle Frames eines Boots in Sende-Reihenfolge + Anzahl CRC-Fehler."""
    files = sorted((int(m.group(1)), p) for p in path.iterdir()
                   if (m := FILE_RE.match(p.name)))
    extractor = dl.FrameExtractor()
    frames: list[dl.DownlinkFrame] = []
    for _, f in files:
        frames.extend(extractor.feed(f.read_bytes()))
    return frames, extractor.crc_errors


def unwrap_times(frames: list[dl.DownlinkFrame]) -> list[int]:
    """16-Bit-TIME (laeuft alle 65,5 s ueber) zu millis() seit Boot entfalten.

    Der Logger startet mit dem Boot, das erste Frame liegt also sicher in den
    ersten 65,5 s. Ein Sprung rueckwaerts um mehr als die halbe Spanne ist ein
    Ueberlauf."""
    out, high, prev = [], 0, None
    for f in frames:
        if prev is not None and f.time_ms < prev and prev - f.time_ms > 0x8000:
            high += 0x10000
        prev = f.time_ms
        out.append(high + f.time_ms)
    return out


# ── Hauptablauf ─────────────────────────────────────────────────────────────────
def convert(root: Path, out_dir: Path) -> None:
    boots = find_boot_dirs(root)
    if not boots:
        sys.exit(f"Keine Boot-Ordner (Bxxxxx) in {root} gefunden.")

    # 1. Alle Frames auf eine gemeinsame Zeitachse (ms ab erstem Boot).
    records = []     # (t_global_ms, boot_label, t_boot_ms, frame)
    events  = []     # (t_global_ms, boot_label, t_boot_ms, event, detail)
    offset = 0
    for boot_no, suffix, path in boots:
        label = f"{boot_no}" + (f"_{suffix}" if suffix else "")
        frames, crc_errors = read_boot(path)
        if not frames:
            print(f"  Boot {label}: keine Frames")
            continue
        times = unwrap_times(frames)
        events.append((offset + times[0], label, times[0], "boot",
                       f"{len(frames)} Frames, {crc_errors} CRC-Fehler"))

        prev_counter = None
        for f, t in zip(frames, times):
            if is_event(f):             # Ereignisse tragen keinen eigenen Zaehlerstand
                records.append((offset + t, label, t, f))
                continue
            if prev_counter is not None:
                lost = (f.counter - prev_counter - 1) & 0xFFFF
                if lost:
                    events.append((offset + t, label, t, "frames_lost", str(lost)))
            prev_counter = f.counter
            records.append((offset + t, label, t, f))

        print(f"  Boot {label}: {len(frames)} Frames, {times[-1] / 1000:.1f} s, "
              f"{crc_errors} CRC-Fehler")
        offset += times[-1]          # naechster Boot schliesst hier an

    # 2. Ereignisse (Zustand, SODS/LO/SOE) und Nullpunkt (erstes SODS).
    #    Bevorzugt aus den ms-genauen EVENT-Frames, sonst aus dem 1-Hz-SYS-Frame.
    has_events = any(is_event(f) for _, _, _, f in records)
    t_zero = None
    if has_events:
        for t_glob, label, t_boot, f in records:
            if not is_event(f):
                continue
            fl = f.fields
            if fl["state_name"] != fl["prev_state_name"]:
                events.append((t_glob, label, t_boot, "state",
                               f"{fl['prev_state_name']} -> {fl['state_name']}"))
            for name, bit in SIGNALS:
                now_on, was_on = bool(fl["signals"] & bit), bool(fl["prev_signals"] & bit)
                if now_on != was_on:
                    events.append((t_glob, label, t_boot, name, "aktiv" if now_on else "inaktiv"))
                if name == "SODS" and now_on and t_zero is None:
                    t_zero = t_glob
        precision = "1 ms"
    else:
        prev = {}
        for t_glob, label, t_boot, f in records:
            if (f.msgid1, f.msgid2) != (dl.SUBSYS_SYS, dl.SYS_STATE):
                continue
            fl = f.fields
            for key, name in (("state_name", "state"), ("sods_active", "SODS"),
                              ("l0_active", "LO"), ("soe_active", "SOE")):
                if key in prev and prev[key] != fl[key]:
                    detail = (f"{prev[key]} -> {fl[key]}" if key == "state_name"
                              else ("aktiv" if fl[key] else "inaktiv"))
                    events.append((t_glob, label, t_boot, name, detail))
                prev[key] = fl[key]
            if fl.get("sods_active") and t_zero is None:
                t_zero = t_glob
        precision = "+-1 s (keine EVENT-Frames)"

    # 3. Nullpunkt
    if t_zero is None:
        t_zero = records[0][0]
        print("  WARNUNG: kein SODS gefunden - Nullpunkt = erstes Frame.")
    else:
        print(f"  Nullpunkt SODS bei {t_zero / 1000:.3f} s (Genauigkeit {precision}).")

    # 4. Je Frame-Typ eine CSV.
    rows: dict[str, list[dict]] = defaultdict(list)
    columns: dict[str, dict] = defaultdict(dict)     # geordnete Spaltenmenge
    for t_glob, label, t_boot, f in records:
        if is_event(f):
            continue                  # stehen in events.csv
        key = (f.msgid1, f.msgid2)
        name = OUTPUT_NAMES.get(key, f"unknown_{f.msgid1:02X}_{f.msgid2:02X}")
        row = {"time_s": round((t_glob - t_zero) / 1000, 3), "boot": label,
               "t_boot_ms": t_boot, "counter": f.counter,
               "status1": f.status1, "status2": f.status2}
        for k, v in f.fields.items():
            row[k] = int(v) if isinstance(v, bool) else v
        rows[name].append(row)
        for k in row:
            columns[name].setdefault(k, None)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(rows.items()):
        write_csv(out_dir / f"{name}.csv", list(columns[name]), data)

    events.sort(key=lambda e: e[0])
    write_csv(out_dir / "events.csv", ["time_s", "boot", "t_boot_ms", "event", "detail"],
              [{"time_s": round((t - t_zero) / 1000, 3), "boot": b, "t_boot_ms": tb,
                "event": e, "detail": d} for t, b, tb, e, d in events])

    print(f"  {len(rows) + 1} CSV-Dateien in {out_dir}/")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", type=Path, help="SD-Karte (Wurzel) oder ein Boot-Ordner Bxxxxx")
    ap.add_argument("-o", "--out", type=Path, default=Path("sd_csv"), help="Zielordner")
    args = ap.parse_args()
    print(f"Lese {args.path} ...")
    convert(args.path, args.out)


if __name__ == "__main__":
    main()

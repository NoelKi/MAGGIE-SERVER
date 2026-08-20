"""
MAGGIE – OBC-State-Store
========================

Hält den aktuellen OBC-Zustand im Speicher und liefert ihn als Snapshot,
der 1:1 dem Interface `ObcSnapshot` der Ground Station entspricht
(MAGGIE-GS/src/app/core/models/maggie.models.ts).

Gefüttert wird der Store aus zwei Quellen:
  * UDP-Paket-Listener — jedes gültige MAGGIE-Paket aktualisiert Sequenz,
    Zeitstempel, Flags und die abgeleitete Flugphase (`note_packet`).
  * serieller RXSM-Downlink — jedes decodierte 20-Byte-Frame hält den OBC
    online, SYS/STATE-Frames liefern zusätzlich den Missionszustand
    (`note_downlink`).

WICHTIG — was das UDP-Protokoll NICHT liefert:
  operation_mode, arm, hrdm, light

Diese Felder existieren im GS-Modell, haben im aktuellen 64-Byte-Paketformat
(app/services/packet_parser.py) aber kein Gegenstück. Sie werden mit
Defaults ausgeliefert, damit der Client-Snapshot vollständig bleibt, und
sind über `update_subsystem()` setzbar, sobald das Protokoll sie mitführt.

Online-Erkennung:
  online == True, solange innerhalb von OBC_ONLINE_TIMEOUT_S ein gültiges
  Paket ODER Downlink-Frame eintraf. Ein Watchdog-Thread kippt das Flag nach
  Ablauf auf False und broadcastet den geänderten Snapshot.
"""

import time
import logging
import threading

from app.extensions import socketio
from app.services.packet_parser import (
    RexusHeader,
    FLAG_LIFTOFF,
    FLAG_SODS,
    FLAG_SOEX,
    FLAG_BURNOUT,
    FLAG_APOGEE,
    FLAG_PARACHUTE,
)

log = logging.getLogger(__name__)

# Ohne Paket innerhalb dieser Zeitspanne gilt der OBC als offline.
OBC_ONLINE_TIMEOUT_S = 5.0

# Taktrate des Watchdogs, der den Offline-Übergang erkennt.
_WATCHDOG_INTERVAL_S = 1.0


def flags_to_dict(flags_byte: int) -> dict:
    """Zerlegt das Flag-Byte des Paket-Headers in das GS-Interface `FlagState`."""
    return {
        "liftoff":   bool(flags_byte & FLAG_LIFTOFF),
        "sods":      bool(flags_byte & FLAG_SODS),
        "soex":      bool(flags_byte & FLAG_SOEX),
        "burnout":   bool(flags_byte & FLAG_BURNOUT),
        "apogee":    bool(flags_byte & FLAG_APOGEE),
        "parachute": bool(flags_byte & FLAG_PARACHUTE),
    }


class ObcState:
    """Thread-sicherer Zustandsspeicher für den OBC."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Aus dem Paket-Header
        self._flags_byte    = 0
        self._flight_phase  = "ground"
        self._last_sequence = 0
        self._timestamp_ms  = 0

        # Aus Heartbeat- bzw. System-Paketen
        self._boot_count     = 0
        self._last_heartbeat = 0.0      # Unix-Zeit, 0 = noch nie
        self._uptime_ms      = 0

        # Online-Erkennung (monotonic — immun gegen Systemzeit-Sprünge)
        self._last_rx_monotonic = 0.0
        self._online = False

        # Missionszustand — kommt aus SYS/STATE-Frames des seriellen Downlinks
        self._mission_state  = "UNKNOWN"
        self._operation_mode = "NONE"
        self._arm = {
            "joint1_position": 0.0, "joint2_position": 0.0,
            "joint1_current": 0.0,  "joint2_current": 0.0,
            "error_code": 0, "status_flags": 0, "updated_at": 0,
        }
        self._hrdm  = {"deployed": False, "locked": False, "updated_at": 0}
        self._light = {"on": False, "updated_at": 0}

    # ── Lesen ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Vollständiger Zustand im Format des GS-Interfaces `ObcSnapshot`."""
        with self._lock:
            return {
                "online":         self._online,
                "mission_state":  self._mission_state,
                "operation_mode": self._operation_mode,
                "flight_phase":   self._flight_phase,
                "flags":          flags_to_dict(self._flags_byte),
                "arm":            dict(self._arm),
                "hrdm":           dict(self._hrdm),
                "light":          dict(self._light),
                "last_heartbeat": self._last_heartbeat,
                "last_sequence":  self._last_sequence,
                "boot_count":     self._boot_count,
                "uptime_ms":      self._uptime_ms,
                "timestamp_ms":   self._timestamp_ms,
            }

    def flags(self) -> dict:
        """Nur die RXSM-Flags (GS-Interface `FlagState`)."""
        with self._lock:
            return flags_to_dict(self._flags_byte)

    # ── Schreiben ────────────────────────────────────────────────────────────

    def note_packet(self, hdr: RexusHeader) -> bool:
        """
        Verbucht ein gültiges Paket.

        Gibt True zurück, wenn sich am Snapshot etwas geändert hat, das die
        GUI sehen muss (Flags, Flugphase oder Online-Übergang) — nur dann
        lohnt ein `obc:state`-Broadcast.
        """
        with self._lock:
            changed = (
                hdr.flags != self._flags_byte
                or hdr.flight_phase != self._flight_phase
                or not self._online
            )

            self._flags_byte    = hdr.flags
            self._flight_phase  = hdr.flight_phase
            self._last_sequence = hdr.sequence
            self._timestamp_ms  = hdr.timestamp_ms

            self._last_rx_monotonic = time.monotonic()
            self._online = True

            return changed

    def note_downlink(self, mission_state: str | None = None) -> bool:
        """
        Verbucht ein gültiges Frame des seriellen RXSM-Downlinks.

        Args:
            mission_state: Zustandsname aus einem SYS/STATE-Frame, sonst None.

        Returns:
            True, wenn sich am Snapshot etwas geändert hat, das die GUI sehen
            muss (Online-Übergang oder Zustandswechsel) — nur dann lohnt ein
            `obc:state`-Broadcast.
        """
        with self._lock:
            changed = not self._online

            if mission_state and mission_state != self._mission_state:
                self._mission_state = mission_state
                changed = True

            self._last_rx_monotonic = time.monotonic()
            self._online = True

            return changed

    def note_heartbeat(self, boot_count: int) -> None:
        """Verbucht einen Heartbeat (Type 0xFF)."""
        with self._lock:
            self._boot_count     = boot_count
            self._last_heartbeat = time.time()

    def note_uptime(self, uptime_s: float) -> None:
        """Übernimmt die Uptime aus einem SYSTEM-Paket."""
        with self._lock:
            self._uptime_ms = int(uptime_s * 1000)

    def update_subsystem(self, name: str, values: dict) -> None:
        """
        Setzt eines der Subsystem-Felder ('arm', 'hrdm', 'light') und stempelt
        `updated_at`. Vorgesehen für den Tag, an dem das Protokoll (oder ein
        Command-ACK) diese Zustände tatsächlich liefert.
        """
        with self._lock:
            target = {"arm": self._arm, "hrdm": self._hrdm, "light": self._light}.get(name)
            if target is None:
                raise ValueError(f"Unbekanntes Subsystem: {name}")
            target.update(values)
            target["updated_at"] = time.time()

    def _expire_if_stale(self) -> bool:
        """Kippt `online` auf False, wenn zu lange kein Paket kam."""
        with self._lock:
            if not self._online:
                return False
            if time.monotonic() - self._last_rx_monotonic <= OBC_ONLINE_TIMEOUT_S:
                return False
            self._online = False
            return True


# Prozessweiter Singleton — Listener und Socket-Handler teilen sich diese Instanz.
obc_state = ObcState()


# ── Broadcast-Helfer ─────────────────────────────────────────────────────────

def emit_state() -> None:
    """Broadcastet den aktuellen Snapshot als `obc:state` an alle Clients."""
    socketio.emit("obc:state", obc_state.snapshot())


def emit_flags() -> None:
    """Broadcastet die RXSM-Flags als `obc:flags`."""
    socketio.emit("obc:flags", obc_state.flags())


# ── Watchdog ─────────────────────────────────────────────────────────────────

_watchdog_lock = threading.Lock()
_watchdog_thread: threading.Thread | None = None


def _watchdog_loop() -> None:
    while True:
        socketio.sleep(_WATCHDOG_INTERVAL_S)
        if obc_state._expire_if_stale():   # noqa: SLF001 — modul-interner Zustand
            log.warning(
                "OBC offline — kein Paket seit %.1f s", OBC_ONLINE_TIMEOUT_S
            )
            emit_state()


def start_state_watchdog() -> None:
    """
    Startet den Offline-Watchdog als Daemon-Thread.

    Sicher mehrfach aufrufbar — ein zweiter Aufruf wird ignoriert.
    """
    with _watchdog_lock:
        # pylint: disable=global-statement
        global _watchdog_thread  # noqa: PLW0603

        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            log.debug("OBC-State-Watchdog läuft bereits — kein zweiter Start")
            return

        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="obc-state-watchdog",
            daemon=True,
        )
        _watchdog_thread.start()
    log.info("OBC-State-Watchdog gestartet (Timeout %.1f s)", OBC_ONLINE_TIMEOUT_S)

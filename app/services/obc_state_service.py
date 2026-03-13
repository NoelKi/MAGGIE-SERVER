"""
MAGGIE – OBC State Service
===========================

Zentrales In-Memory State-Tracking für den On-Board Computer.

Hält den aktuellen Zustand (MissionState, Flags, Heartbeat,
ARM-Status, HRDM-Status, Light-Status) und stellt ihn per
Getter-Funktionen für Routes und Socket.IO-Events bereit.

Der ``packet_listener`` aktualisiert diesen Service bei jedem
empfangenen Paket.  Routes lesen daraus den aktuellen Stand.

Thread-sicher über ein ``threading.Lock``.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field, asdict

# ── Mission-States (Spiegel der OBC-Enum) ────────────────────────────────────

MISSION_STATES = (
    "STARTUP",
    "PREFLIGHT_CHECK",
    "STANDBY",
    "EXPERIMENT",
    "TESTING",
    "HARDWARE_TESTING",
    "SHUTDOWN",
)

OPERATION_MODES = (
    "NONE",
    "FLIGHT",
    "TESTING",
    "HARDWARE_TEST",
)

# ── Command-IDs (Server → OBC) ──────────────────────────────────────────────

CMD_SET_STATE       = 0x01
CMD_HRDM_DEPLOY     = 0x02
CMD_HRDM_LOCK       = 0x03
CMD_LIGHT_ON        = 0x04
CMD_LIGHT_OFF       = 0x05
CMD_ARM_ESTOP       = 0x06
CMD_SELECT_MODE     = 0x07

# ── Datenklassen ─────────────────────────────────────────────────────────────


@dataclass
class FlagState:
    """RXSM-Flag-Bits aus dem OBC-Paket-Header."""
    liftoff:    bool = False
    sods:       bool = False
    soex:       bool = False
    burnout:    bool = False
    apogee:     bool = False
    parachute:  bool = False


@dataclass
class ArmState:
    """Roboterarm-Status (vom STM32 via OBC)."""
    joint1_position: float = 0.0
    joint2_position: float = 0.0
    joint1_current:  float = 0.0   # mA
    joint2_current:  float = 0.0   # mA
    error_code:      int   = 0
    status_flags:    int   = 0
    updated_at:      float = 0.0


@dataclass
class HrdmState:
    """HRDM (Hold-Release-Deploy Mechanism) Status."""
    deployed: bool  = False
    locked:   bool  = True
    updated_at: float = 0.0


@dataclass
class LightState:
    """Light-Unit Status."""
    on:         bool  = False
    updated_at: float = 0.0


@dataclass
class ObcSnapshot:
    """Kompletter Snapshot des OBC-Zustands — wird als JSON an Clients geschickt."""
    online:           bool        = False
    mission_state:    str         = "STARTUP"
    operation_mode:   str         = "NONE"
    flight_phase:     str         = "ground"
    flags:            FlagState   = field(default_factory=FlagState)
    arm:              ArmState    = field(default_factory=ArmState)
    hrdm:             HrdmState   = field(default_factory=HrdmState)
    light:            LightState  = field(default_factory=LightState)
    last_heartbeat:   float       = 0.0
    last_sequence:    int         = 0
    boot_count:       int         = 0
    uptime_ms:        int         = 0
    timestamp_ms:     int         = 0

    def to_dict(self) -> dict:
        """Flaches Dict für JSON-Serialisierung."""
        d = asdict(self)
        # Unix-Timestamps auf 3 Dezimalstellen runden
        for key in ("last_heartbeat",):
            d[key] = round(d[key], 3)
        d["arm"]["updated_at"] = round(d["arm"]["updated_at"], 3)
        d["hrdm"]["updated_at"] = round(d["hrdm"]["updated_at"], 3)
        d["light"]["updated_at"] = round(d["light"]["updated_at"], 3)
        return d


# ── Singleton State-Service ──────────────────────────────────────────────────

class ObcStateService:
    """
    Thread-sicherer In-Memory State-Manager für den OBC.

    Nutzung::

        from app.services.obc_state_service import obc_state

        # Schreiben (vom packet_listener):
        obc_state.update_heartbeat(seq=42, boot_count=3)
        obc_state.update_flags(0x07)

        # Lesen (von Routes / Socket.IO):
        snap = obc_state.snapshot()
    """

    def __init__(self, heartbeat_timeout: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._state = ObcSnapshot()
        self._heartbeat_timeout = heartbeat_timeout

    # ── Konfiguration ─────────────────────────────────────────────

    def set_heartbeat_timeout(self, seconds: float) -> None:
        with self._lock:
            self._heartbeat_timeout = seconds

    # ── Lesen ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Gibt einen JSON-fähigen Snapshot des kompletten OBC-Zustands zurück."""
        with self._lock:
            self._state.online = self._is_online()
            return self._state.to_dict()

    @property
    def online(self) -> bool:
        with self._lock:
            return self._is_online()

    @property
    def mission_state(self) -> str:
        with self._lock:
            return self._state.mission_state

    @property
    def flight_phase(self) -> str:
        with self._lock:
            return self._state.flight_phase

    # ── Schreiben (vom packet_listener aufgerufen) ────────────────

    def update_heartbeat(self, seq: int, boot_count: int) -> None:
        """Aktualisiert Heartbeat-Informationen."""
        with self._lock:
            self._state.last_heartbeat = time.time()
            self._state.last_sequence = seq
            self._state.boot_count = boot_count

    def update_header(self, seq: int, timestamp_ms: int, flags: int, flight_phase: str) -> None:
        """Aktualisiert Header-Informationen aus jedem eingehenden Paket."""
        with self._lock:
            self._state.last_sequence = seq
            self._state.timestamp_ms = timestamp_ms
            self._state.flight_phase = flight_phase
            self._state.last_heartbeat = time.time()
            self._update_flags(flags)

    def update_flags(self, raw_flags: int) -> None:
        """Aktualisiert die RXSM-Flags aus dem Paket-Header."""
        with self._lock:
            self._update_flags(raw_flags)

    def update_arm(
        self,
        j1_pos: float, j2_pos: float,
        j1_cur: float, j2_cur: float,
        error_code: int = 0, status_flags: int = 0,
    ) -> None:
        """Aktualisiert Roboterarm-Status."""
        with self._lock:
            arm = self._state.arm
            arm.joint1_position = j1_pos
            arm.joint2_position = j2_pos
            arm.joint1_current = j1_cur
            arm.joint2_current = j2_cur
            arm.error_code = error_code
            arm.status_flags = status_flags
            arm.updated_at = time.time()

    def update_hrdm(self, deployed: bool, locked: bool) -> None:
        """Aktualisiert HRDM-Status."""
        with self._lock:
            self._state.hrdm.deployed = deployed
            self._state.hrdm.locked = locked
            self._state.hrdm.updated_at = time.time()

    def update_light(self, on: bool) -> None:
        """Aktualisiert Light-Unit-Status."""
        with self._lock:
            self._state.light.on = on
            self._state.light.updated_at = time.time()

    def update_mission_state(self, state: str) -> None:
        """Setzt den Mission-State (z.B. nach ACK vom OBC)."""
        if state in MISSION_STATES:
            with self._lock:
                self._state.mission_state = state

    def update_operation_mode(self, mode: str) -> None:
        """Setzt den Operation-Mode (z.B. nach ACK vom OBC)."""
        if mode in OPERATION_MODES:
            with self._lock:
                self._state.operation_mode = mode

    def update_system(self, uptime_ms: float) -> None:
        """Aktualisiert System-Telemetrie."""
        with self._lock:
            self._state.uptime_ms = int(uptime_ms)

    # ── Intern ────────────────────────────────────────────────────

    def _update_flags(self, raw: int) -> None:
        """Setzt Flag-Bits (Lock muss gehalten werden!)."""
        f = self._state.flags
        f.liftoff   = bool(raw & 0x01)
        f.sods      = bool(raw & 0x02)
        f.soex      = bool(raw & 0x04)
        f.burnout   = bool(raw & 0x08)
        f.apogee    = bool(raw & 0x10)
        f.parachute = bool(raw & 0x20)

    def _is_online(self) -> bool:
        """Prüft, ob der OBC noch lebt (Lock muss gehalten werden!)."""
        if self._state.last_heartbeat == 0.0:
            return False
        return (time.time() - self._state.last_heartbeat) < self._heartbeat_timeout


# ── Modul-Level Singleton ────────────────────────────────────────────────────
obc_state = ObcStateService()

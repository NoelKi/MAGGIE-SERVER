"""
MAGGIE – OBC Status Routes
============================

REST-Endpunkte zum Abfragen des aktuellen OBC-Zustands.

Endpoints:
  GET /api/obc/status       — Kompletter OBC-Snapshot (State, Flags, Arm, HRDM …)
  GET /api/obc/flags        — Nur RXSM-Flag-Bits
  GET /api/obc/arm          — Nur Arm-Status
"""

from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from app.services.obc_state_service import obc_state

status_bp = Blueprint("status", __name__)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/obc/status  — Kompletter Snapshot
# ──────────────────────────────────────────────────────────────────────────────

@status_bp.route("/obc/status", methods=["GET"])
@jwt_required()
def obc_status():
    """
    Gibt den kompletten OBC-Zustand als JSON zurück.

    Beinhaltet:
    - online (bool) — OBC erreichbar?
    - mission_state — aktueller State (STARTUP … SHUTDOWN)
    - operation_mode — NONE / FLIGHT / TESTING / HARDWARE_TEST
    - flight_phase — ground / ascent / sods / experiment / coasting / apogee / descent
    - flags — liftoff, sods, soex, burnout, apogee, parachute
    - arm — Joint-Positionen, Ströme, Fehler
    - hrdm — deployed, locked
    - light — on/off
    - last_heartbeat, last_sequence, boot_count, uptime_ms, timestamp_ms
    """
    return jsonify(obc_state.snapshot()), 200


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/obc/flags  — RXSM-Flags
# ──────────────────────────────────────────────────────────────────────────────

@status_bp.route("/obc/flags", methods=["GET"])
@jwt_required()
def obc_flags():
    """
    Gibt nur die RXSM-Flag-Bits zurück.

    Response: { "liftoff": false, "sods": false, "soex": true, … }
    """
    snap = obc_state.snapshot()
    return jsonify(snap["flags"]), 200


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/obc/arm  — Roboterarm-Status
# ──────────────────────────────────────────────────────────────────────────────

@status_bp.route("/obc/arm", methods=["GET"])
@jwt_required()
def obc_arm():
    """
    Gibt den Roboterarm-Status zurück.

    Response: {
        "joint1_position": 45.2,
        "joint2_position": -12.3,
        "joint1_current": 234.5,
        "joint2_current": 189.1,
        "error_code": 0,
        "status_flags": 0
    }
    """
    snap = obc_state.snapshot()
    return jsonify(snap["arm"]), 200

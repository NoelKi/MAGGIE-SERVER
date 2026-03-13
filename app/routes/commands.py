"""
MAGGIE – Command Routes
========================

REST-Endpunkte zum Senden von Befehlen an den OBC.

Alle Routen erfordern JWT-Authentifizierung und Rolle ``admin`` oder ``operator``.

Endpoints:
  POST /api/command/state          — Mission-State setzen
  POST /api/command/mode           — Operation-Mode wählen (FLIGHT/TESTING/HARDWARE_TEST)
  POST /api/command/hrdm/deploy    — HRDM auslösen
  POST /api/command/hrdm/lock      — HRDM verriegeln
  POST /api/command/light          — Light Unit ein/aus
  POST /api/command/arm/estop      — Roboterarm Emergency Stop
"""

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt

from app.services.command_service import cmd_service
from app.services.obc_state_service import (
    obc_state,
    MISSION_STATES,
)
from app.services.stream_service import emit_command_ack, emit_obc_state

commands_bp = Blueprint("commands", __name__)


def _require_operator():
    """Prüft, ob der aktuelle User admin oder operator ist."""
    claims = get_jwt()
    if claims.get("role") not in ("admin", "operator"):
        return jsonify({"error": "Forbidden — admin or operator required"}), 403
    return None


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/command/state  — Mission-State setzen
# ──────────────────────────────────────────────────────────────────────────────

@commands_bp.route("/command/state", methods=["POST"])
@jwt_required()
def set_state():
    """
    Setzt den Mission-State des OBC.

    Body: { "state": "EXPERIMENT" }
    Erlaubte Werte: STARTUP, PREFLIGHT_CHECK, STANDBY, EXPERIMENT,
                    TESTING, HARDWARE_TESTING, SHUTDOWN
    """
    err = _require_operator()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    state = body.get("state", "").strip().upper()

    if state not in MISSION_STATES:
        return jsonify({
            "error": f"Ungültiger State: '{state}'",
            "allowed": list(MISSION_STATES),
        }), 400

    ok = cmd_service.send_set_state(state)

    if ok:
        # Optimistisches Update — wird bei ACK vom OBC bestätigt
        obc_state.update_mission_state(state)
        emit_obc_state()
        emit_command_ack(0x01, True, f"State → {state}")

    return jsonify({
        "success": ok,
        "state": state,
    }), 200 if ok else 502


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/command/mode  — Operation-Mode wählen
# ──────────────────────────────────────────────────────────────────────────────

@commands_bp.route("/command/mode", methods=["POST"])
@jwt_required()
def select_mode():
    """
    Wählt den Operation-Mode (nur aus STANDBY).

    Body: { "mode": "FLIGHT" }
    Erlaubte Werte: FLIGHT, TESTING, HARDWARE_TEST
    """
    err = _require_operator()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "").strip().upper()

    if mode not in ("FLIGHT", "TESTING", "HARDWARE_TEST"):
        return jsonify({
            "error": f"Ungültiger Mode: '{mode}'",
            "allowed": ["FLIGHT", "TESTING", "HARDWARE_TEST"],
        }), 400

    # Prüfe ob OBC in STANDBY ist
    if obc_state.mission_state != "STANDBY":
        return jsonify({
            "error": "Mode-Auswahl nur in STANDBY möglich",
            "current_state": obc_state.mission_state,
        }), 409

    ok = cmd_service.send_select_mode(mode)

    if ok:
        obc_state.update_operation_mode(mode)
        emit_obc_state()
        emit_command_ack(0x07, True, f"Mode → {mode}")

    return jsonify({
        "success": ok,
        "mode": mode,
    }), 200 if ok else 502


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/command/hrdm/deploy  — HRDM auslösen
# ──────────────────────────────────────────────────────────────────────────────

@commands_bp.route("/command/hrdm/deploy", methods=["POST"])
@jwt_required()
def hrdm_deploy():
    """
    Löst den HRDM (Hold-Release-Deploy Mechanism) aus.
    Gibt den Roboterarm frei.
    """
    err = _require_operator()
    if err:
        return err

    ok = cmd_service.send_hrdm_deploy()

    if ok:
        obc_state.update_hrdm(deployed=True, locked=False)
        emit_obc_state()
        emit_command_ack(0x02, True, "HRDM deployed")

    return jsonify({"success": ok, "action": "deploy"}), 200 if ok else 502


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/command/hrdm/lock  — HRDM verriegeln
# ──────────────────────────────────────────────────────────────────────────────

@commands_bp.route("/command/hrdm/lock", methods=["POST"])
@jwt_required()
def hrdm_lock():
    """
    Verriegelt den HRDM — sichert den Roboterarm in Transportposition.
    """
    err = _require_operator()
    if err:
        return err

    ok = cmd_service.send_hrdm_lock()

    if ok:
        obc_state.update_hrdm(deployed=False, locked=True)
        emit_obc_state()
        emit_command_ack(0x03, True, "HRDM locked")

    return jsonify({"success": ok, "action": "lock"}), 200 if ok else 502


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/command/light  — Light Unit ein/aus
# ──────────────────────────────────────────────────────────────────────────────

@commands_bp.route("/command/light", methods=["POST"])
@jwt_required()
def light_control():
    """
    Schaltet die Light Unit ein oder aus.

    Body: { "on": true }   oder   { "on": false }
    """
    err = _require_operator()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    on = body.get("on")

    if on is None or not isinstance(on, bool):
        return jsonify({"error": "'on' (bool) is required"}), 400

    ok = (cmd_service.send_light_on() if on else cmd_service.send_light_off())

    if ok:
        obc_state.update_light(on)
        emit_obc_state()
        emit_command_ack(0x04 if on else 0x05, True, f"Light {'ON' if on else 'OFF'}")

    return jsonify({"success": ok, "light": on}), 200 if ok else 502


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/command/arm/estop  — Roboterarm Emergency Stop
# ──────────────────────────────────────────────────────────────────────────────

@commands_bp.route("/command/arm/estop", methods=["POST"])
@jwt_required()
def arm_emergency_stop():
    """
    Sendet einen Emergency-Stop an den Roboterarm (via OBC → CAN → STM32).
    Stoppt alle Motoren sofort.
    """
    err = _require_operator()
    if err:
        return err

    ok = cmd_service.send_arm_estop()

    if ok:
        emit_command_ack(0x06, True, "ARM Emergency Stop sent")
        emit_obc_state()

    return jsonify({"success": ok, "action": "arm_estop"}), 200 if ok else 502

"""
MAGGIE – Telecommand Routes
===========================

REST-Schnittstelle zum Senden von Telecommands an den OBC über das RXSM.
Die Ground Station postet hierhin; der Server verpackt das Kommando als
SDC-Nutzlast (rxsm_tc_parser.build_sdc_packet) und schreibt es auf den
seriellen TC-Port (tc_uplink).

  POST /api/command/motor        — Motor steuern (on/off/half_turn)
  GET  /api/command/status       — TC-Uplink-Verbindungsstatus
  POST /api/command/connect      — TC-Port (neu)starten
  POST /api/command/disconnect   — TC-Port trennen

Motor-Command-Frame (6 Bytes, in der SDC-Nutzlast) — identisch zur OBC-Firmware
(include/hal/uplink_hal.hpp):

  [START(0x7E)][OPCODE][ARG_hi][ARG_lo][CRC8][END(0x7F)]
  CRC-8 (poly 0x07) über OPCODE..ARG_lo.
"""

from flask import Blueprint, request, jsonify

from app.extensions import socketio
from app.services.tc_uplink import tc

command_bp = Blueprint("command", __name__)

# ── Motor-Command-Frame (muss zu uplink_hal.hpp passen) ─────────────────────
UL_START = 0x7E
UL_END   = 0x7F

# OPCODE (MotorOpcode)
MOTOR_OPCODES = {
    "off":       0x00,
    "on":        0x01,
    "half_turn": 0x02,
}

# UART-Ziel-Adresse am RXSM (0-7), auf der der OBC lauscht. Ggf. an den
# physischen Aufbau des Test Modules anpassen.
DEFAULT_MOTOR_DEST = 0


def _crc8(data: bytes) -> int:
    """CRC-8/SMBUS (poly 0x07, init 0x00) — identisch zu telemetry_hal/uplink_hal."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _build_motor_payload(opcode: int, arg: int = 0) -> bytes:
    """Baut das 6-Byte-Motor-Command-Frame für die SDC-Nutzlast."""
    body = bytes([opcode & 0xFF, (arg >> 8) & 0xFF, arg & 0xFF])
    return bytes([UL_START]) + body + bytes([_crc8(body), UL_END])


@command_bp.route("/command/motor", methods=["POST"])
def motor():
    """
    Steuert den Motor.

    Body (JSON): { "action": "on" | "off" | "half_turn", "dest": <0-7, optional> }
    """
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip().lower()

    if action not in MOTOR_OPCODES:
        return jsonify({
            "error": f"'action' muss eines von {sorted(MOTOR_OPCODES)} sein",
        }), 400

    try:
        dest = int(body.get("dest", DEFAULT_MOTOR_DEST))
    except (TypeError, ValueError):
        return jsonify({"error": "'dest' must be an integer"}), 400
    if not 0 <= dest <= 7:
        return jsonify({"error": "'dest' must be in range 0..7"}), 400

    payload = _build_motor_payload(MOTOR_OPCODES[action])

    try:
        result = tc.send_sdc(dest, payload)
    except RuntimeError as exc:
        # Auch der Fehlschlag geht an alle Clients — nicht nur an den Absender
        socketio.emit("command:ack", {
            "cmd_id":  -1,
            "success": False,
            "detail":  f"motor {action}: {exc}",
        })
        return jsonify({"error": str(exc)}), 503

    socketio.emit("command:ack", {
        "cmd_id":  result["mcnt"],
        "success": True,
        "detail":  f"motor {action} → dest {result['dest']}",
    })

    return jsonify({
        "status":   "ok",
        "action":   action,
        "motor":    action,
        "mcnt":     result["mcnt"],
        "dest":     result["dest"],
        "sent_hex": result["sent_hex"],
    }), 201


@command_bp.route("/command/status", methods=["GET"])
def status():
    """TC-Uplink-Verbindungsstatus."""
    return jsonify(tc.snapshot()), 200


@command_bp.route("/command/connect", methods=["POST"])
def connect():
    """
    (Neu-)Startet den seriellen TC-Port zum RXSM.

    Body (JSON): { "port": "/dev/tty.usbserial-XXXX", "baud": 38400 }
    """
    body = request.get_json(silent=True) or {}
    port = (body.get("port") or "").strip()
    if not port:
        return jsonify({"error": "'port' is required"}), 400

    try:
        baud = int(body.get("baud", 38400))
    except (TypeError, ValueError):
        return jsonify({"error": "'baud' must be an integer"}), 400

    try:
        tc.start(port, baud)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    return jsonify({"status": "ok", "port": port, "baud": baud}), 200


@command_bp.route("/command/disconnect", methods=["POST"])
def disconnect():
    """Trennt den seriellen TC-Port."""
    tc.stop()
    return jsonify({"status": "ok"}), 200

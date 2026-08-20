"""
MAGGIE – Telecommand Routes
===========================

REST-Schnittstelle zum Senden von Telecommands an den OBC über das RXSM.
Die Ground Station postet hierhin; der Server verpackt das Kommando als
SDC-Nutzlast (rxsm_tc_parser.build_sdc_packet) und schreibt es auf den
seriellen TC-Port (tc_uplink).

  POST /api/command/motor        — HDRM-Motor steuern
  POST /api/command/test         — Bodentest-Zustand betreten/verlassen
  POST /api/command/abort        — Abbruch (Aktoren stoppen)
  GET  /api/command/status       — TC-Uplink-Verbindungsstatus
  POST /api/command/connect      — TC-Port (neu)starten
  POST /api/command/disconnect   — TC-Port trennen

Command-Frame (6 Bytes, in der SDC-Nutzlast) — identisch zur OBC-Firmware
(include/hal/uplink_hal.hpp):

  [START(0x7E)][OPCODE][ARG_hi][ARG_lo][CRC8][END(0x7F)]
  CRC-8 (poly 0x07) über OPCODE..ARG_lo.

Motorkommandos führt der OBC NUR im Zustand TEST aus (siehe StateMachine) —
vorher muss POST /api/command/test {"action":"enter"} kommen. Einzige Ausnahme
ist "off": das Ausschalten ist ein Sicherheitskommando und immer erlaubt.
"""

from flask import Blueprint, request, jsonify

from app.extensions import socketio
from app.services.tc_uplink import tc

command_bp = Blueprint("command", __name__)

# ── Command-Frame (muss zu uplink_hal.hpp passen) ───────────────────────────
UL_START = 0x7E
UL_END   = 0x7F

# OPCODE (UplinkOpcode) — Aktoren, nur im TEST-Zustand ausgeführt
# 0x02 (MOTOR_HALF_TURN) ist Altbestand und im OBC identisch zu half_turn_fwd;
# er wird hier nicht mehr angeboten.
MOTOR_OPCODES = {
    "off":           0x00,   # Motor aus
    "on":            0x01,   # Motor dauerhaft an
    "half_turn_fwd": 0x03,   # halbe Umdrehung vorwärts (relativ +180°)
    "half_turn_rev": 0x04,   # halbe Umdrehung rückwärts (relativ -180°)
    "zero":          0x05,   # aktuelle Position = Nullpunkt
}

# OPCODE (UplinkOpcode) — Zustandsmaschine, immer erlaubt
TEST_OPCODES = {
    "enter": 0x10,
    "exit":  0x11,
}

OPCODE_ABORT = 0x1F

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


def _build_payload(opcode: int, arg: int = 0) -> bytes:
    """Baut das 6-Byte-Command-Frame für die SDC-Nutzlast."""
    body = bytes([opcode & 0xFF, (arg >> 8) & 0xFF, arg & 0xFF])
    return bytes([UL_START]) + body + bytes([_crc8(body), UL_END])


def _read_dest(body: dict):
    """Liest 'dest' aus dem Request-Body. Gibt (dest, fehler_response) zurück."""
    try:
        dest = int(body.get("dest", DEFAULT_MOTOR_DEST))
    except (TypeError, ValueError):
        return None, (jsonify({"error": "'dest' must be an integer"}), 400)
    if not 0 <= dest <= 7:
        return None, (jsonify({"error": "'dest' must be in range 0..7"}), 400)
    return dest, None


def _send(opcode: int, dest: int, label: str):
    """
    Sendet ein Telecommand und broadcastet das Ergebnis als 'command:ack'.

    Returns:
        Flask-Response-Tupel — 201 bei Erfolg, 503 wenn der TC-Port zu ist.
    """
    payload = _build_payload(opcode)

    try:
        result = tc.send_sdc(dest, payload)
    except RuntimeError as exc:
        # Auch der Fehlschlag geht an alle Clients — nicht nur an den Absender
        socketio.emit("command:ack", {
            "cmd_id":  -1,
            "success": False,
            "detail":  f"{label}: {exc}",
        })
        return jsonify({"error": str(exc)}), 503

    socketio.emit("command:ack", {
        "cmd_id":  result["mcnt"],
        "success": True,
        "detail":  f"{label} → dest {result['dest']}",
    })

    return jsonify({
        "status":   "ok",
        "action":   label,
        "opcode":   opcode,
        "mcnt":     result["mcnt"],
        "dest":     result["dest"],
        "sent_hex": result["sent_hex"],
    }), 201


@command_bp.route("/command/motor", methods=["POST"])
def motor():
    """
    Steuert den HDRM-Motor.

    Body (JSON):
      { "action": "on"|"off"|"half_turn_fwd"|"half_turn_rev"|"zero",
        "dest": <0-7, optional> }

    Der OBC führt diese Kommandos nur im TEST-Zustand aus ("off" immer).
    """
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip().lower()

    if action not in MOTOR_OPCODES:
        return jsonify({
            "error": f"'action' muss eines von {sorted(MOTOR_OPCODES)} sein",
        }), 400

    dest, err = _read_dest(body)
    if err:
        return err

    return _send(MOTOR_OPCODES[action], dest, f"motor {action}")


@command_bp.route("/command/test", methods=["POST"])
def test_state():
    """
    Schaltet den OBC in den Bodentest-Zustand (TEST) oder zurück.

    Body (JSON): { "action": "enter" | "exit", "dest": <0-7, optional> }

    TEST ist der einzige Zustand, in dem der OBC Aktor-Telecommands ausführt.
    'enter' wird vom OBC nur aus PRE_LAUNCH akzeptiert.
    """
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip().lower()

    if action not in TEST_OPCODES:
        return jsonify({
            "error": f"'action' muss eines von {sorted(TEST_OPCODES)} sein",
        }), 400

    dest, err = _read_dest(body)
    if err:
        return err

    return _send(TEST_OPCODES[action], dest, f"test {action}")


@command_bp.route("/command/abort", methods=["POST"])
def abort():
    """
    Missionsabbruch: der OBC geht nach ABORT und stoppt alle Aktoren.

    Body (JSON): { "dest": <0-7, optional> }
    """
    dest, err = _read_dest(request.get_json(silent=True) or {})
    if err:
        return err

    return _send(OPCODE_ABORT, dest, "abort")


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

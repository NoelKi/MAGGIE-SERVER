"""
MAGGIE – Telecommand Routes
===========================

REST-Schnittstelle zum Senden von Telecommands an den OBC über das RXSM.
Die Ground Station postet hierhin; der Server verpackt das Kommando als
SDC-Nutzlast (rxsm_tc_parser.build_sdc_packet) und schreibt es auf den
seriellen TC-Port (tc_uplink).

  POST /api/command/motor        — Motor drehen/stoppen (Open Loop)
  POST /api/command/force/tare   — Kraftsensor 1 und/oder 2 nullen
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
# 0x02..0x04 (MOTOR_HALF_TURN / HALF_TURN_FWD / HALF_TURN_REV) sind stillgelegt:
# Die Positionsregelung im OBC ist entfallen, der Encoder ist nur noch Sensor.
# Werte nicht neu vergeben.
MOTOR_OPCODES = {
    "off":  0x00,   # Motor aus
    "on":   0x01,   # Motor drehen, ARG = Geschwindigkeit -255..+255
    "zero": 0x05,   # Encoder-Zähler auf 0 setzen
    "turn": 0x06,   # Drehung um ARG Grad RELATIV, OBC stoppt am Encoder-Ziel
    "goto": 0x07,   # Fahrt auf ARG Grad ABSOLUT (bezogen auf die Encoder-Null)
}

# Aktionen, die 'angle' brauchen und im ARG-Feld transportieren.
MOTOR_ANGLE_ACTIONS = ("turn", "goto")

# Grenzen für 'speed' bei action="on" — entspricht dem PWM-Bereich der
# MotorHAL (setSpeed clamped auf ±255). 0 heißt: OBC nimmt DEFAULT_ON_SPEED.
MOTOR_SPEED_MIN = -255
MOTOR_SPEED_MAX = 255

# Kleinster PWM-Betrag, mit dem der Motor anläuft (MotorHAL::MIN_DRIVE_SPEED).
# Am Aufbau gemessen: unter ~150 dreht er selbst ohne Last nicht mehr, unter
# Last liegt die Schwelle höher. Der OBC hebt kleinere Werte ohnehin an — hier
# wird schon abgewiesen, damit ein zu kleiner Wert auffällt, statt still
# verändert zu werden.
MOTOR_MIN_DRIVE = 220

# Grenzen für 'angle' bei action="turn". ±3600° = 10 Umdrehungen; alles darüber
# ist am Tischaufbau eher ein Tippfehler als eine Absicht.
MOTOR_ANGLE_MIN = -3600
MOTOR_ANGLE_MAX = 3600

# OPCODE (UplinkOpcode) — Zustandsmaschine, immer erlaubt
TEST_OPCODES = {
    "enter": 0x10,
    "exit":  0x11,
}

OPCODE_ABORT = 0x1F

# OPCODE (UplinkOpcode) — Sensoren (0x2x), zustandsunabhängig.
# Tarieren bewegt keinen Aktor und liegt deshalb bewusst nicht hinter der
# TEST-Sperre.
OPCODE_FORCE_TARE = 0x20

# ARG von FORCE_TARE: 0 = beide Kraftsensoren, 1 = Sensor 1, 2 = Sensor 2.
FORCE_SENSOR_MIN = 0
FORCE_SENSOR_MAX = 2

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


def _read_ranged_int(body: dict, key: str, lo: int, hi: int, default: int = 0):
    """
    Liest einen optionalen ganzzahligen Parameter aus dem Request-Body.

    Ein vertippter Wert soll auffallen und nicht still in den Bereich geklemmt
    werden — deshalb 400 statt clamp.

    Returns:
        (wert, fehler_response) — fehler_response ist None, wenn alles passt.
    """
    if key not in body or body[key] is None:
        return default, None
    try:
        value = int(body[key])
    except (TypeError, ValueError):
        return None, (jsonify({"error": f"'{key}' must be an integer"}), 400)
    if not lo <= value <= hi:
        return None, (jsonify({
            "error": f"'{key}' must be in range {lo}..{hi}",
        }), 400)
    return value, None


def _send(opcode: int, dest: int, label: str, arg: int = 0):
    """
    Sendet ein Telecommand und broadcastet das Ergebnis als 'command:ack'.

    Args:
        arg: 16-Bit-Argument im Command-Frame (bei MOTOR_ON die Geschwindigkeit).

    Returns:
        Flask-Response-Tupel — 201 bei Erfolg, 503 wenn der TC-Port zu ist.
    """
    payload = _build_payload(opcode, arg)

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
        "arg":      arg,
        "mcnt":     result["mcnt"],
        "dest":     result["dest"],
        "sent_hex": result["sent_hex"],
    }), 201


@command_bp.route("/command/motor", methods=["POST"])
def motor():
    """
    Steuert den Motor.

    Body (JSON):
      { "action": "on"|"off"|"zero"|"turn"|"goto",
        "speed":  <-255..255, optional, nur bei "on">,
        "angle":  <-3600..3600, bei "turn" und "goto">,
        "dest":   <0-7, optional> }

    'on' läuft ungeregelt, bis 'off' kommt. 'speed' ist der PWM-Stellwert; das
    Vorzeichen gibt die Drehrichtung vor. Fehlt er (oder ist 0), nimmt der OBC
    seine DEFAULT_ON_SPEED vorwärts.

    'turn' dreht um 'angle' Grad RELATIV zur aktuellen Position.
    'goto' fährt auf 'angle' Grad ABSOLUT, bezogen auf die Encoder-Null. Für
    wiederholtes Auf/Zu ist 'goto' die richtige Wahl: Bei 'turn' addiert sich
    der Überschwinger jeder Fahrt auf und die Nullage wandert.

    Beide stoppen selbst, sobald der Encoder das Ziel erreicht — mit fester
    Geschwindigkeit (TURN_SPEED), 'speed' gilt dort nicht. Ohne angeschlossenen
    Encoder verwirft der OBC die Kommandos.

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

    # ARG bedeutet je nach Aktion etwas anderes und bleibt sonst 0.
    if action == "on":
        arg, err = _read_ranged_int(body, "speed", MOTOR_SPEED_MIN, MOTOR_SPEED_MAX)
        # 0 ist erlaubt und heißt "OBC nimmt seine DEFAULT_ON_SPEED".
        if not err and arg != 0 and abs(arg) < MOTOR_MIN_DRIVE:
            return jsonify({
                "error": f"'speed' muss betragsmäßig mindestens {MOTOR_MIN_DRIVE} "
                         f"sein (oder 0 für den Default) — darunter läuft der "
                         f"Motor nicht an.",
            }), 400
        label = f"motor on (speed={arg})"
    elif action in MOTOR_ANGLE_ACTIONS:
        arg, err = _read_ranged_int(body, "angle", MOTOR_ANGLE_MIN, MOTOR_ANGLE_MAX)
        # 'goto 0' ist gueltig (Fahrt auf die Nullage), 'turn 0' waere sinnlos.
        if not err and arg == 0 and action == "turn":
            return jsonify({"error": "'angle' ist bei action='turn' erforderlich"}), 400
        if not err and "angle" not in body:
            return jsonify({
                "error": f"'angle' ist bei action='{action}' erforderlich",
            }), 400
        bezug = "absolut" if action == "goto" else "relativ"
        label = f"motor {action} ({arg} Grad {bezug})"
    else:
        arg, err, label = 0, None, f"motor {action}"

    if err:
        return err

    return _send(MOTOR_OPCODES[action], dest, label, arg)


@command_bp.route("/command/force/tare", methods=["POST"])
def force_tare():
    """
    Setzt den Nullpunkt der Kraftsensoren neu.

    Body (JSON): { "sensor": 0|1|2, optional, "dest": <0-7, optional> }
      sensor = 0 (Standard) → beide, 1 → Kraftsensor 1, 2 → Kraftsensor 2

    Die Wiegezellen müssen dabei UNBELASTET sein — der gemessene Mittelwert
    wird zum neuen Nullpunkt. Der OBC mittelt dafür über 8 Wandlungen (~0,8 s
    je Sensor) und meldet den Erfolg über das Flag 'force_tared' bzw.
    'force2_tared' im jeweiligen FORCE-Frame.

    Bei Kraftsensor 2 wiegt das schwerer als bei Sensor 1: Aus dessen vier
    Zellen rechnet der Server einen Kraftvektor. Ein unter Last gesetzter
    Nullpunkt verschiebt nicht nur einen Kanal, sondern verdreht den Vektor.

    Zustandsunabhängig: Tarieren bewegt keinen Aktor.
    """
    body = request.get_json(silent=True) or {}

    dest, err = _read_dest(body)
    if err:
        return err

    sensor, err = _read_ranged_int(body, "sensor", FORCE_SENSOR_MIN, FORCE_SENSOR_MAX)
    if err:
        return err

    label = "force tare (beide)" if sensor == 0 else f"force tare (Sensor {sensor})"
    return _send(OPCODE_FORCE_TARE, dest, label, sensor)


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

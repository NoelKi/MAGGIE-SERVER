from app import create_app
from app.extensions import socketio

app = create_app()

if __name__ == "__main__":
    print("\n========================================")
    print("  MAGGIE Ground Station Server")
    print("  REXUS Programme — v0.2.0")
    print("========================================")
    print("  GET  /api/health            — Health check")
    print("  ── Telemetry (HTTP) ───────────────────")
    print("  POST /api/telemetry         — Write data point")
    print("  POST /api/telemetry/batch   — Write multiple points")
    print("  GET  /api/telemetry         — Query data points")
    print("  GET  /api/telemetry/ping    — InfluxDB status")
    print("  ── OBC UDP-Interface ──────────────────")
    print("  UDP :9000                   — Binary OBC packets (64 Bytes)")
    print("  ── RXSM Downlink (Serial) ─────────────")
    print("  GET  /api/downlink/status   — Serial link status")
    print("  GET  /api/downlink/raw      — Live raw bytes (hex)")
    print("  GET  /api/downlink/ports    — List serial ports")
    print("  POST /api/downlink/connect  — Connect to a serial port")
    print("  ── Realtime (Socket.IO) ───────────────")
    print("  WS   /socket.io             — obc:state / obc:heartbeat /")
    print("                                obc:flags / command:ack /")
    print("                                telemetry:imu|environment|system")
    print("  Running on http://localhost:3000")
    print("========================================\n")

    # socketio.run statt app.run: Werkzeug allein kann kein WebSocket-Upgrade
    # und würde /socket.io-Verbindungen mit ECONNRESET abbrechen.
    # allow_unsafe_werkzeug=True: Dev-Server bewusst erlaubt — für den Flug
    # gehört hier ein produktiver WSGI-Server (gunicorn + gevent) hin.
    # debug=app.debug: derselbe Wert, den create_app() für den Listener-Guard
    # benutzt hat (config.settings.Config.DEBUG) — sonst laufen Reloader und
    # Guard auseinander.
    socketio.run(
        app,
        host="0.0.0.0",
        port=3000,
        debug=app.debug,
        allow_unsafe_werkzeug=True,
    )

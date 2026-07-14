from app import create_app

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
    print("  Running on http://localhost:3000")
    print("========================================\n")

    # threaded=True: paralleles Request-Handling — eine langsame InfluxDB-Query
    # blockiert so nicht das Live-Frame-Polling der GUI.
    app.run(host="0.0.0.0", port=3000, debug=True, threaded=True)

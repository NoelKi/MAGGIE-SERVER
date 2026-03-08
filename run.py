from app import create_app

app = create_app()

if __name__ == "__main__":
    print("\n========================================")
    print("  MAGGIE Ground Station Server")
    print("  REXUS Programme — v0.2.0")
    print("========================================")
    print("  POST /api/auth/login        — Login")
    print("  GET  /api/auth/me           — Current user")
    print("  GET  /api/health            — Health check")
    print("  ── Telemetry (HTTP) ───────────────────")
    print("  POST /api/telemetry         — Write data point")
    print("  POST /api/telemetry/batch   — Write multiple points")
    print("  GET  /api/telemetry         — Query data points")
    print("  GET  /api/telemetry/ping    — InfluxDB status")
    print("  ── OBC UDP-Interface ──────────────────")
    print("  UDP :9000                   — Binary OBC packets (64 Bytes)")
    print("  Running on http://localhost:3000")
    print("========================================\n")

    app.run(host="0.0.0.0", port=3000, debug=True)

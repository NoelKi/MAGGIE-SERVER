from app import create_app

app = create_app()

if __name__ == "__main__":
    print("\n========================================")
    print("  MAGGIE Ground Station Server")
    print("  REXUS Programme — v0.1.0")
    print("========================================")
    print("  POST /api/auth/login   — Login")
    print("  GET  /api/auth/me      — Current user")
    print("  GET  /api/health       — Health check")
    print("  Running on http://localhost:5050")
    print("========================================\n")

    app.run(host="0.0.0.0", port=5050, debug=False)

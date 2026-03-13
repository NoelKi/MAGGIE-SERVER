from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager

from app.extensions import db, migrate, socketio

jwt = JWTManager()


def create_app():
    """
    Flask App Factory.

    Initialisiert alle Extensions (SQLAlchemy, Migrate, JWT, CORS, Socket.IO),
    registriert Blueprints und startet den UDP-Listener für OBC-Pakete.
    """
    app = Flask(__name__)

    # Config laden
    app.config.from_object("config.settings.Config")

    # Extensions initialisieren
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    jwt.init_app(app)
    db.init_app(app)
    migrate.init_app(app, db)

    # Socket.IO initialisieren (CORS erlaubt alle Origins für WebSocket)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="eventlet")

    # Stream-Service mit Socket.IO-Instanz verbinden
    from app.services.stream_service import init_socketio
    init_socketio(socketio)

    # OBC State Service konfigurieren
    from app.services.obc_state_service import obc_state
    obc_state.set_heartbeat_timeout(
        app.config.get("OBC_HEARTBEAT_TIMEOUT", 5.0)
    )

    # Command Service initialisieren
    from app.services.command_service import cmd_service
    cmd_service.init(app)

    # Modelle importieren, damit Flask-Migrate sie kennt
    from app.models import user  # noqa: F401

    # Blueprints registrieren
    from app.routes.auth import auth_bp
    from app.routes.health import health_bp
    from app.routes.telemetry import telemetry_bp
    from app.routes.commands import commands_bp
    from app.routes.status import status_bp

    app.register_blueprint(auth_bp,      url_prefix="/api/auth")
    app.register_blueprint(health_bp,    url_prefix="/api")
    app.register_blueprint(telemetry_bp, url_prefix="/api")
    app.register_blueprint(commands_bp,  url_prefix="/api")
    app.register_blueprint(status_bp,    url_prefix="/api")

    # ── Datenbank-Tabellen + Default-User ───────────────────────────
    with app.app_context():
        db.create_all()
        from app.services.auth_service import seed_default_users
        seed_default_users()
        print("✓ DB-Tabellen + Standard-User geprüft / angelegt.")

    # CLI-Shortcut bleibt erhalten
    @app.cli.command("seed-users")
    def seed_users_cmd():
        """Legt Standard-User an (admin + operator), falls keine existieren."""
        from app.services.auth_service import seed_default_users
        seed_default_users()
        print("✓ Standard-User geprüft / angelegt.")

    # ── UDP-Listener für OBC-Pakete (REXUS-Protokoll) ─────────────────
    import os as _os
    if not app.debug or _os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        from app.services.packet_listener import start_udp_listener
        start_udp_listener(app)

    return app


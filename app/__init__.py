from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager

from app.extensions import db, migrate

jwt = JWTManager()


def create_app():
    """
    Flask App Factory.

    Initialisiert alle Extensions (SQLAlchemy, Migrate, JWT, CORS),
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

    # Modelle importieren, damit Flask-Migrate sie kennt
    from app.models import user  # noqa: F401

    # Blueprints registrieren
    from app.routes.auth import auth_bp
    from app.routes.health import health_bp
    from app.routes.telemetry import telemetry_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(health_bp, url_prefix="/api")
    app.register_blueprint(telemetry_bp, url_prefix="/api")

    # Standard-User anlegen (benötigt App-Kontext + laufende DB)
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


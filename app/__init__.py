from flask import Flask
from flask_cors import CORS

from app.extensions import db, migrate


def create_app():
    """
    Flask App Factory.

    Initialisiert alle Extensions (SQLAlchemy, Migrate, CORS),
    registriert Blueprints und startet den UDP-Listener für OBC-Pakete.
    """
    app = Flask(__name__)

    # Config laden
    app.config.from_object("config.settings.Config")

    # Extensions initialisieren
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    db.init_app(app)
    migrate.init_app(app, db)

    # Blueprints registrieren
    from app.routes.health import health_bp
    from app.routes.telemetry import telemetry_bp
    from app.routes.downlink import downlink_bp

    app.register_blueprint(health_bp, url_prefix="/api")
    app.register_blueprint(telemetry_bp, url_prefix="/api")
    app.register_blueprint(downlink_bp, url_prefix="/api")

    # ── Listener nur einmal starten (nicht im Werkzeug-Reloader-Parent) ─
    import os as _os
    if not app.debug or _os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        # UDP-Listener für OBC-Pakete (MAGGIE-Protokoll)
        from app.services.packet_listener import start_udp_listener
        start_udp_listener(app)

        # Serieller RXSM-Downlink (Rohbytes → Live-Hexdump)
        from app.services.serial_listener import init_serial_listener
        init_serial_listener(app)

    return app

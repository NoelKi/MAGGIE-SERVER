from flask import Flask
from flask_cors import CORS
from flask_jwt_extended import JWTManager

jwt = JWTManager()


def create_app():
    """Flask App Factory."""
    app = Flask(__name__)

    # Config laden
    app.config.from_object("config.settings.Config")

    # Extensions
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    jwt.init_app(app)

    # Blueprints registrieren
    from app.routes.auth import auth_bp
    from app.routes.health import health_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(health_bp, url_prefix="/api")

    return app

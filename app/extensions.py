"""
Flask-Extensions – zentrale Instanzen (circular-import-safe).

Importiere hier db und migrate, nie direkt in app/__init__.py anlegen.
Alle Modelle und Blueprints importieren von hier:
  from app.extensions import db
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db      = SQLAlchemy()
migrate = Migrate()

"""
Flask-Extensions – zentrale Instanzen (circular-import-safe).

Importiere hier db, migrate und socketio, nie direkt in app/__init__.py anlegen.
Alle Modelle und Blueprints importieren von hier:
  from app.extensions import db
  from app.extensions import socketio
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_socketio import SocketIO

db       = SQLAlchemy()
migrate  = Migrate()
socketio = SocketIO()

"""
Flask-Extensions – zentrale Instanzen (circular-import-safe).

Importiere hier db und migrate, nie direkt in app/__init__.py anlegen.
Alle Modelle und Blueprints importieren von hier:
  from app.extensions import db
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_socketio import SocketIO

db      = SQLAlchemy()
migrate = Migrate()

# async_mode="threading": passt zum threaded Werkzeug-Server und zu den
# Daemon-Threads (UDP-Listener, Serial-Listener) — kein eventlet/gevent nötig.
# Emits aus diesen Threads sind damit erlaubt.
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")

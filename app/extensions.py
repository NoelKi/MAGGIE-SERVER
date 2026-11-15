"""
Flask-Extensions – zentrale Instanzen (circular-import-safe).

Importiere hier socketio, nie direkt in app/__init__.py anlegen:
  from app.extensions import socketio
"""

from flask_socketio import SocketIO

# async_mode="threading": passt zum threaded Werkzeug-Server und zu den
# Daemon-Threads (UDP-Listener, Serial-Listener) — kein eventlet/gevent nötig.
# Emits aus diesen Threads sind damit erlaubt.
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")

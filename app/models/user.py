"""
MAGGIE – User-Modell
====================

Speichert Benutzerkonten in PostgreSQL.
Passwörter werden niemals im Klartext gespeichert — nur als bcrypt-Hash.

Tabelle: users
  id          SERIAL PRIMARY KEY
  username    VARCHAR(64) UNIQUE NOT NULL
  email       VARCHAR(256) UNIQUE
  password_hash  TEXT NOT NULL
  role        VARCHAR(32) NOT NULL DEFAULT 'operator'
  created_at  TIMESTAMP WITH TIME ZONE DEFAULT now()
  last_login  TIMESTAMP WITH TIME ZONE
  is_active   BOOLEAN DEFAULT TRUE
"""

from datetime import datetime, timezone
import bcrypt
from app.extensions import db


class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email         = db.Column(db.String(256), unique=True, nullable=True)
    password_hash = db.Column(db.Text, nullable=False)
    role          = db.Column(db.String(32), nullable=False, default="operator")
    created_at    = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_login    = db.Column(db.DateTime(timezone=True), nullable=True)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)

    # ── Passwort-Methoden ──────────────────────────────────────────────────
    def set_password(self, password: str) -> None:
        """Setzt den bcrypt-Hash des Passworts."""
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, password: str) -> bool:
        """Gibt True zurück wenn das Passwort korrekt ist."""
        return bcrypt.checkpw(
            password.encode("utf-8"),
            self.password_hash.encode("utf-8"),
        )

    # ── Serialisierung ─────────────────────────────────────────────────────
    def to_dict(self, include_private: bool = False) -> dict:
        """
        Gibt das User-Objekt als Dictionary zurück.
        Passwort-Hash wird NIEMALS zurückgegeben.
        """
        data = {
            "id":         self.id,
            "username":   self.username,
            "role":       self.role,
            "is_active":  self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }
        if include_private:
            data["email"] = self.email
        return data

    def __repr__(self) -> str:
        return f"<User {self.username!r} role={self.role!r}>"

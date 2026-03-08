"""
MAGGIE – Auth-Service (PostgreSQL-Backend)
==========================================

Alle User-Operationen laufen über SQLAlchemy.
Passwörter werden ausschließlich als bcrypt-Hash gespeichert.
"""

from datetime import datetime, timezone
from app.extensions import db
from app.models.user import User


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> User | None:
    """Sucht einen User anhand des Benutzernamens. Gibt None zurück wenn nicht gefunden."""
    return db.session.execute(
        db.select(User).where(User.username == username)
    ).scalar_one_or_none()


def get_user_by_id(user_id: int) -> User | None:
    """Sucht einen User anhand der Datenbank-ID. Gibt None zurück wenn nicht gefunden."""
    return db.session.get(User, user_id)


# ── Öffentliche API ───────────────────────────────────────────────────────────

def register_user(
    username: str,
    password: str,
    role: str = "operator",
    email: str | None = None,
) -> tuple[User | None, str]:
    """
    Legt einen neuen User an.

    Returns:
        (user, "")               bei Erfolg
        (None, "fehlermeldung")  bei Fehler
    """
    if get_user_by_username(username):
        return None, f"Benutzername '{username}' ist bereits vergeben."

    user = User(username=username, role=role, email=email)  # type: ignore[call-arg]
    user.set_password(password)

    db.session.add(user)
    db.session.commit()
    return user, ""


def authenticate(username: str, password: str) -> User | None:
    """
    Prüft Credentials.

    Returns:
        User-Objekt bei Erfolg, None bei falschen Credentials oder
        deaktiviertem Account.
    """
    user = get_user_by_username(username)
    if user is None or not user.is_active:
        return None
    if not user.check_password(password):
        return None

    # last_login aktualisieren
    user.last_login = datetime.now(timezone.utc)
    db.session.commit()
    return user


def get_user(username: str) -> dict | None:
    """Gibt User-Dict (ohne Passwort) zurück oder None."""
    user = get_user_by_username(username)
    return user.to_dict(include_private=True) if user else None


def update_password(username: str, new_password: str) -> bool:
    """Setzt ein neues Passwort. Gibt True bei Erfolg zurück."""
    user = get_user_by_username(username)
    if not user:
        return False
    user.set_password(new_password)
    db.session.commit()
    return True


def deactivate_user(username: str) -> bool:
    """Deaktiviert einen Account (kein Login mehr möglich)."""
    user = get_user_by_username(username)
    if not user:
        return False
    user.is_active = False
    db.session.commit()
    return True


def seed_default_users() -> None:
    """
    Legt Standard-User an, falls die Tabelle leer ist.
    Wird beim App-Start aufgerufen.
    """
    count = db.session.execute(db.select(db.func.count(User.id))).scalar()
    if count == 0:
        register_user("admin",    "maggie2026", role="admin")
        register_user("operator", "rexus",      role="operator")

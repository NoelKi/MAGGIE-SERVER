import bcrypt

# ──────────────────────────────────────────────────────────────────────
# Simpler In-Memory User Store
# Für Produktion: durch Datenbank ersetzen
# ──────────────────────────────────────────────────────────────────────

_users: dict[str, dict] = {}


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def seed_default_users():
    """Erstellt Standard-Benutzer falls noch keine existieren."""
    if not _users:
        register_user("admin", "maggie2026", role="admin")
        register_user("operator", "rexus", role="operator")


def register_user(username: str, password: str, role: str = "operator") -> bool:
    """Neuen Benutzer registrieren."""
    if username in _users:
        return False

    _users[username] = {
        "password": _hash_password(password),
        "role": role,
    }
    return True


def authenticate(username: str, password: str) -> dict | None:
    """Prüft Credentials, gibt User-Dict oder None zurück."""
    user = _users.get(username)
    if user and _check_password(password, user["password"]):
        return {"username": username, "role": user["role"]}
    return None


def get_user(username: str) -> dict | None:
    """User ohne Passwort zurückgeben."""
    user = _users.get(username)
    if user:
        return {"username": username, "role": user["role"]}
    return None

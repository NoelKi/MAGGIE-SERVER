import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Basis-Konfiguration."""
    SECRET_KEY = os.getenv("SECRET_KEY", "fallback-secret")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "fallback-jwt-secret")
    JWT_ACCESS_TOKEN_EXPIRES = 3600  # 1 Stunde

    # ── PostgreSQL – User-Daten ───────────────────────────────────────
    POSTGRES_USER     = os.getenv("POSTGRES_USER", "maggie")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "maggie_secret")
    POSTGRES_HOST     = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT     = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DB       = os.getenv("POSTGRES_DB", "maggie_users")

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"postgresql+psycopg2://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,          # Verbindung vor Nutzung prüfen
        "pool_recycle": 1800,           # Verbindungen alle 30 Min erneuern
    }

    # ── InfluxDB ──────────────────────────────────────────────────────
    INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
    INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN", "")
    INFLUX_ORG    = os.getenv("INFLUX_ORG", "maggie")
    INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "telemetry")

    # ── OBC UDP-Interface ─────────────────────────────────────────────
    UDP_HOST    = os.getenv("UDP_HOST", "0.0.0.0")
    UDP_PORT    = int(os.getenv("UDP_PORT", "9000"))
    UDP_TIMEOUT = float(os.getenv("UDP_TIMEOUT", "2.0"))


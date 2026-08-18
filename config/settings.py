import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Basis-Konfiguration."""
    SECRET_KEY = os.getenv("SECRET_KEY", "fallback-secret")

    # ── PostgreSQL ────────────────────────────────────────────────────
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

    # ── RXSM Downlink (serieller Port) ────────────────────────────────
    # RXSM-Downlink kommt als serieller Stream (RS-232/RS-422/USB) an.
    # SERIAL_PORT leer lassen → Port wird zur Laufzeit in der GUI gewählt.
    #   macOS:  /dev/tty.usbserial-XXXX
    #   Linux:  /dev/ttyUSB0
    #   Windows: COM3
    SERIAL_PORT      = os.getenv("SERIAL_PORT", "")
    SERIAL_BAUD      = int(os.getenv("SERIAL_BAUD", "38400"))   # RXSM Standard: 38400 8N1
    SERIAL_AUTOSTART = os.getenv("SERIAL_AUTOSTART", "true").lower() == "true"
    # Größe des In-Memory-Ringpuffers für den Live-Hexdump (Bytes)
    SERIAL_BUFFER_BYTES = int(os.getenv("SERIAL_BUFFER_BYTES", str(256 * 1024)))

    # ── RXSM Telecommand-Uplink (separater serieller TX-Port) ─────────────
    # Sendet Telecommands (z.B. Motor-Steuerung) an das RXSM-(Test-)Modul,
    # das sie über die UART an den OBC weiterreicht. Eigener Port, getrennt
    # vom read-only Downlink oben. TC_SERIAL_PORT leer lassen → zur Laufzeit
    # über /api/command/connect wählen.
    TC_SERIAL_PORT = os.getenv("TC_SERIAL_PORT", "")
    TC_SERIAL_BAUD = int(os.getenv("TC_SERIAL_BAUD", "38400"))
    TC_AUTOSTART   = os.getenv("TC_AUTOSTART", "true").lower() == "true"


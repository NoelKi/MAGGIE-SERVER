import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Basis-Konfiguration."""
    SECRET_KEY = os.getenv("SECRET_KEY", "fallback-secret")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "fallback-jwt-secret")
    JWT_ACCESS_TOKEN_EXPIRES = 3600  # 1 Stunde

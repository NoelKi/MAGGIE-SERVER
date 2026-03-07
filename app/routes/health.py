from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    """
    GET /api/health
    Einfacher Health-Check für den Server.
    """
    return jsonify({
        "status": "ok",
        "service": "MAGGIE Ground Station Server",
        "version": "0.1.0",
    }), 200

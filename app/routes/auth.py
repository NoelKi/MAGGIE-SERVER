from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token,
    get_jwt_identity,
    get_jwt,
    jwt_required,
)
from app.services.auth_service import authenticate, get_user, register_user

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["POST"])
def login():
    """
    POST /api/auth/login
    Body: { "username": "admin", "password": "maggie2026" }
    Returns: { "access_token": "...", "user": { ... } }
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    user = authenticate(username, password)
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_access_token(
        identity=user.username,
        additional_claims={"role": user.role},
    )

    return jsonify({
        "access_token": token,
        "user": user.to_dict(),
    }), 200


@auth_bp.route("/register", methods=["POST"])
@jwt_required()
def register():
    """
    POST /api/auth/register   (nur für Admins)
    Header: Authorization: Bearer <token>
    Body: { "username": "...", "password": "...", "role": "operator", "email": "..." }
    """
    claims = get_jwt()
    if claims.get("role") != "admin":
        return jsonify({"error": "Admin-Rechte erforderlich"}), 403

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    username = body.get("username", "").strip()
    password = body.get("password", "")
    role     = body.get("role", "operator")
    email    = body.get("email")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    if role not in ("admin", "operator", "viewer"):
        return jsonify({"error": "Ungültige Rolle. Erlaubt: admin, operator, viewer"}), 400

    user, err = register_user(username, password, role=role, email=email)
    if err or user is None:
        return jsonify({"error": err or "Unbekannter Fehler"}), 409

    return jsonify({"message": "User angelegt", "user": user.to_dict()}), 201


@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    """
    GET /api/auth/me
    Header: Authorization: Bearer <token>
    Returns: { "user": { ... } }
    """
    current_user = get_jwt_identity()
    user = get_user(current_user)
    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({"user": user}), 200


from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token,
    jwt_required,
    get_jwt_identity,
)
from app.services.auth_service import authenticate, seed_default_users, get_user

auth_bp = Blueprint("auth", __name__)

# Default-User beim ersten Import anlegen
seed_default_users()


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
        identity=username,
        additional_claims={"role": user["role"]},
    )

    return jsonify({
        "access_token": token,
        "user": user,
    }), 200


@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    """
    GET /api/auth/me
    Header: Authorization: Bearer <token>
    Returns: { "user": { "username": "...", "role": "..." } }
    """
    current_user = get_jwt_identity()
    user = get_user(current_user)
    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({"user": user}), 200

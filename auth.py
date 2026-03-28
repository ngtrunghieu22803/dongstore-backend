"""
Auth utilities: JWT, sessions, password hashing.
"""
import jwt
import hashlib
import datetime
import uuid
from functools import wraps
from flask import request, jsonify, current_app
from db import get_db


def hash_token(token: str) -> str:
    """Băm token bằng SHA-256 rồi lấy hex ngắn để lưu vào DB."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def decode_jwt(token: str):
    """Decode JWT, trả về payload hoặc None nếu token không hợp lệ/hết hạn."""
    try:
        return jwt.decode(token, current_app.config['JWT_SECRET'], algorithms=['HS256'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def is_active_session_token(token: str, user_id: str = None) -> bool:
    """Kiểm tra token còn session hợp lệ trong DB hay không."""
    conn = get_db()
    cur = conn.cursor()
    if user_id:
        cur.execute(
            'SELECT 1 FROM sessions WHERE user_id = %s AND token_hash = %s AND expires_at > NOW() LIMIT 1',
            (user_id, hash_token(token)),
        )
    else:
        cur.execute(
            'SELECT 1 FROM sessions WHERE token_hash = %s AND expires_at > NOW() LIMIT 1',
            (hash_token(token),),
        )
    row = cur.fetchone()
    cur.close()
    return bool(row)


def generate_tokens(user_id: str, email: str, role: str = 'user'):
    """Tạo cặp access + refresh token."""
    now = datetime.datetime.utcnow()

    access_payload = {
        'userId': user_id,
        'email': email,
        'role': role,
        'type': 'access',
        'exp': now + datetime.timedelta(hours=1),
        'iat': now,
    }
    refresh_payload = {
        'userId': user_id,
        'email': email,
        'role': role,
        'type': 'refresh',
        'exp': now + datetime.timedelta(hours=168),
        'iat': now,
    }
    secret = current_app.config['JWT_SECRET']
    access_token = jwt.encode(access_payload, secret, algorithm='HS256')
    refresh_token = jwt.encode(refresh_payload, secret, algorithm='HS256')
    return access_token, refresh_token


def save_session(user_id: str, token: str, hours: int):
    """Lưu session vào DB với thời hạn hours giờ."""
    conn = get_db()
    cur = conn.cursor()
    session_id = str(uuid.uuid4())
    token_hash = hash_token(token)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    cur.execute(
        'INSERT INTO sessions (id, user_id, token_hash, expires_at) VALUES (%s, %s, %s, %s)',
        (session_id, user_id, token_hash, expires_at),
    )
    conn.commit()
    cur.close()


def _decode_token(token: str):
    """Decode access token và kiểm tra session. Trả về payload dict hoặc None."""
    payload = decode_jwt(token)
    if not payload:
        return None

    if payload.get('type') != 'access':
        return None

    if not is_active_session_token(token, payload.get('userId')):
        return None

    return payload


def require_auth(f):
    """Decorator: yêu cầu Bearer token hợp lệ, đặt request.user dict.
    Chấp nhận token từ header 'Authorization: Bearer <token>' HOẶC query param '?token=<token>'
    (dùng cho <audio> tag không gửi được custom headers)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        token = None
        if auth.startswith('Bearer '):
            token = auth[7:]
        elif request.args.get('token'):
            token = request.args.get('token')

        if not token:
            return jsonify({'error': 'No token provided'}), 401

        payload = _decode_token(token)
        if not payload:
            return jsonify({'error': 'Token invalid or revoked'}), 401

        # request.user là dict — KHÔNG dùng getattr(obj, 'attr') vì dict không có attribute
        request.user = {
            'id': payload['userId'],
            'email': payload['email'],
            'role': payload.get('role', 'user'),
        }
        return f(*args, **kwargs)

    return decorated

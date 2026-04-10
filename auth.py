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


def _request_device_id() -> str:
    """
    Lấy device id từ header.
    Ưu tiên X-Device-Id (desktop app), fallback X-Client-Device.
    """
    v = (request.headers.get('X-Device-Id') or request.headers.get('X-Client-Device') or '').strip()
    if v:
        return v[:200]

    # Fallback cho web/client chưa gửi header:
    # tạo pseudo-device-id ổn định theo IP + User-Agent để tránh lưu NULL.
    # (Mục tiêu chính: admin theo dõi session không còn "(none)".)
    ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote_addr or 'unknown-ip')
    ua = (request.headers.get('User-Agent') or 'unknown-ua').strip()
    raw = f'{ip}|{ua}'
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]
    return f'web-{digest}'


def hash_token(token: str) -> str:
    """Băm token bằng SHA-256 rồi lấy hex ngắn để lưu vào DB."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def decode_jwt(token: str):
    """Decode JWT, trả về payload hoặc None nếu token không hợp lệ/hết hạn."""
    try:
        return jwt.decode(token, current_app.config['JWT_SECRET'], algorithms=['HS256'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def is_active_session_token(token: str, user_id: str = None, device_id: str = None) -> bool:
    """Kiểm tra token còn session hợp lệ trong DB hay không."""
    conn = get_db()
    cur = conn.cursor()
    base_sql = 'SELECT 1 FROM sessions WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > NOW()'
    params = [hash_token(token)]
    if user_id:
        base_sql += ' AND user_id = %s'
        params.append(user_id)
    if device_id:
        base_sql += ' AND device_id = %s'
        params.append(device_id)
    base_sql += ' LIMIT 1'
    cur.execute(base_sql, tuple(params))
    row = cur.fetchone()
    cur.close()
    return bool(row)


def generate_tokens(user_id: str, email: str, role: str = 'user', device_id: str = None):
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
    if device_id:
        access_payload['deviceId'] = device_id
        refresh_payload['deviceId'] = device_id
    secret = current_app.config['JWT_SECRET']
    access_token = jwt.encode(access_payload, secret, algorithm='HS256')
    refresh_token = jwt.encode(refresh_payload, secret, algorithm='HS256')
    return access_token, refresh_token


def save_session(user_id: str, token: str, hours: int, device_id: str = None):
    """Lưu session vào DB với thời hạn hours giờ."""
    conn = get_db()
    cur = conn.cursor()
    session_id = str(uuid.uuid4())
    token_hash = hash_token(token)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    cur.execute(
        'INSERT INTO sessions (id, user_id, token_hash, device_id, expires_at) VALUES (%s, %s, %s, %s, %s)',
        (session_id, user_id, token_hash, (device_id or None), expires_at),
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

    token_device = (payload.get('deviceId') or '').strip()
    req_device = _request_device_id()
    if token_device and token_device != req_device:
        return None

    if not is_active_session_token(token, payload.get('userId'), token_device or None):
        return None

    return payload


def require_auth(f):
    """Decorator: yêu cầu Bearer token hợp lệ, đặt request.user dict.
    Chỉ chấp nhận token từ header 'Authorization: Bearer <token>'."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Cho preflight CORS đi qua (không yêu cầu token).
        if request.method == 'OPTIONS':
            return ('', 204)

        auth = request.headers.get('Authorization', '')
        token = None
        if auth.startswith('Bearer '):
            token = auth[7:]

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

from os import environ as os_environ

from flask import Blueprint, request, jsonify, redirect, current_app, make_response
from urllib.parse import urlencode
import bcrypt
import hashlib
import uuid
import requests
import re
import time
from threading import Lock
from typing import Any, Tuple
from db import get_db
from auth import (
    generate_tokens,
    save_session,
    require_auth,
    hash_token,
    decode_jwt,
    is_active_session_token,
    _request_device_id,
)
from security_audit import log_security_event

auth_bp = Blueprint('auth', __name__)

# Refresh JWT trong cookie HttpOnly (frontend cross-origin HTTPS dùng SameSite=None; dev HTTP dùng Lax + body fallback).
REFRESH_COOKIE_NAME = 'dong_refresh'
_REFRESH_MAX_AGE_SEC = 60 * 60 * 168  # 168h khớp generate_tokens refresh

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_AUTH_RATE = {}
_AUTH_RATE_LOCK = Lock()

_LOGIN_FAILS = {}
_LOGIN_LOCKOUT = {}
_LOGIN_LOCK = Lock()
FAIL_WINDOW_SEC = 15 * 60
MAX_LOGIN_FAILS = 5
LOCKOUT_SEC = 15 * 60
SUSPECT_FAILS_BEFORE_SESSION_REVOKE = 3


def _require_admin_key() -> Tuple[bool, Any]:
    """
    Bảo vệ endpoint cấp token theo ADMIN_KEY.
    Mục tiêu: app/desktop có thể "đăng nhập thay" (impersonate) một user
    mà không cần biết password của user đó.
    """
    admin_key = (current_app.config.get('ADMIN_KEY') or '').strip()
    if not admin_key:
        return False, jsonify({'error': 'ADMIN_KEY is not configured'}), 500
    return True, admin_key


def _login_key(raw: str) -> str:
    return (raw or "").strip().lower()[:128]


def _is_account_locked(key: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        until = _LOGIN_LOCKOUT.get(key)
        if until and until > now:
            return True
        if until and until <= now:
            del _LOGIN_LOCKOUT[key]
        return False


def _record_login_failure_existing_user(key: str) -> None:
    now = time.time()
    with _LOGIN_LOCK:
        cutoff = now - FAIL_WINDOW_SEC
        fails = [t for t in _LOGIN_FAILS.get(key, []) if t > cutoff]
        fails.append(now)
        _LOGIN_FAILS[key] = fails
        if len(fails) >= MAX_LOGIN_FAILS:
            _LOGIN_LOCKOUT[key] = now + LOCKOUT_SEC
            log_security_event(
                "account_locked",
                message="Khóa đăng nhập tạm thời sau nhiều lần sai mật khẩu",
                extra={"key_hash": hashlib.sha256(key.encode()).hexdigest()[:16]},
            )


def _fail_count_in_window(key: str) -> int:
    now = time.time()
    with _LOGIN_LOCK:
        cutoff = now - FAIL_WINDOW_SEC
        return len([t for t in _LOGIN_FAILS.get(key, []) if t > cutoff])


def _clear_login_failures(key: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(key, None)
        _LOGIN_LOCKOUT.pop(key, None)


def _hit_rate(bucket: str, limit: int, window_sec: int) -> bool:
    ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote_addr or 'unknown')
    key = f'{bucket}:{ip}'
    now = int(time.time())
    with _AUTH_RATE_LOCK:
        entries = [t for t in _AUTH_RATE.get(key, []) if t > now - window_sec]
        if len(entries) >= limit:
            _AUTH_RATE[key] = entries
            return False
        entries.append(now)
        _AUTH_RATE[key] = entries
        return True


def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or ''))


def _is_strong_password(password: str) -> bool:
    if len(password) < 8:
        return False
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    return has_upper and has_lower and has_digit


def _refresh_cookie_flags():
    if os_environ.get('FORCE_HTTPS', '').strip() == '1':
        return True, 'None'
    proto = (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    if proto == 'https':
        return True, 'None'
    if getattr(request, 'is_secure', False):
        return True, 'None'
    if os_environ.get('PREFERRED_URL_SCHEME', '').strip().lower() == 'https':
        return True, 'None'
    return False, 'Lax'


def _attach_refresh_cookie(response, refresh_token: str):
    secure, samesite = _refresh_cookie_flags()
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=_REFRESH_MAX_AGE_SEC,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path='/',
    )
    return response


def _clear_refresh_cookie(response):
    secure, samesite = _refresh_cookie_flags()
    response.delete_cookie(REFRESH_COOKIE_NAME, path='/', samesite=samesite, secure=secure)
    return response


# ─── Email/Password Auth ───────────────────────────────────────────────────────

@auth_bp.route('/register', methods=['POST'])
def register():
    if not _hit_rate('register', 20, 300):
        return jsonify({'error': 'Quá nhiều yêu cầu, vui lòng thử lại sau'}), 429
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    name = data.get('name', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email và mật khẩu là bắt buộc'}), 400

    if not _is_valid_email(email):
        return jsonify({'error': 'Email không hợp lệ'}), 400

    if not _is_strong_password(password):
        return jsonify({'error': 'Mật khẩu phải từ 8 ký tự, gồm chữ hoa, chữ thường và số'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM users WHERE email = %s', (email,))
    if cur.fetchone():
        return jsonify({'error': 'Email đã được sử dụng'}), 409

    user_id = str(uuid.uuid4())
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    cur.execute(
        'INSERT INTO users (id, email, password, name) VALUES (%s, %s, %s, %s)',
        (user_id, email, hashed, name)
    )
    conn.commit()

    user = {'id': user_id, 'email': email, 'name': name, 'role': 'user'}
    device_id = _request_device_id()
    access_token, refresh_token = generate_tokens(user_id, email, 'user', device_id=device_id or None)
    save_session(user_id, access_token, 1, device_id=device_id or None)
    save_session(user_id, refresh_token, 168, device_id=device_id or None)

    log_security_event(
        'register_success',
        message='Đăng ký tài khoản thành công',
        user_id=user_id,
    )

    resp = make_response(
        jsonify({
            'accessToken': access_token,
            'refreshToken': refresh_token,
            'user': user,
        }),
        201,
    )
    _attach_refresh_cookie(resp, refresh_token)
    return resp


@auth_bp.route('/login', methods=['POST'])
def login():
    if not _hit_rate('login', 20, 300):
        return jsonify({'error': 'Quá nhiều yêu cầu, vui lòng thử lại sau'}), 429
    data = request.get_json(silent=True) or {}
    email_or_name = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not email_or_name or not password:
        return jsonify({'error': 'Tên đăng nhập/email và mật khẩu là bắt buộc'}), 400

    lk = _login_key(email_or_name)
    if _is_account_locked(lk):
        log_security_event(
            "login_blocked_locked",
            message="Đăng nhập bị từ chối (tài khoản tạm khóa)",
            extra={"key_hash": hashlib.sha256(lk.encode()).hexdigest()[:16]},
        )
        return jsonify({
            'error': 'Đăng nhập tạm thời bị khóa do nhập sai mật khẩu nhiều lần. Vui lòng thử lại sau.',
        }), 429

    conn = get_db()
    cur = conn.cursor()
    # Tìm theo email hoặc name (không phân biệt hoa thường)
    cur.execute(
        'SELECT * FROM users WHERE LOWER(email) = %s OR LOWER(name) = %s',
        (email_or_name, email_or_name)
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        log_security_event(
            "login_fail_unknown_user",
            extra={"key_hash": hashlib.sha256(lk.encode()).hexdigest()[:16]},
        )
        return jsonify({'error': 'Tên đăng nhập/email hoặc mật khẩu không đúng'}), 401

    pw_stored = row['password']
    if isinstance(pw_stored, str):
        pw_stored = pw_stored.encode('utf-8')
    elif isinstance(pw_stored, (bytes, bytearray, memoryview)):
        pw_stored = bytes(pw_stored)

    pwd_bytes = password.encode('utf-8')
    match = bcrypt.checkpw(pwd_bytes, pw_stored)

    if not match:
        _record_login_failure_existing_user(lk)
        log_security_event(
            "login_fail_password",
            message="Sai mật khẩu",
            user_id=row['id'],
            extra={"key_hash": hashlib.sha256(lk.encode()).hexdigest()[:16]},
        )
        cur.close()
        conn.close()
        return jsonify({'error': 'Tên đăng nhập/email hoặc mật khẩu không đúng'}), 401

    prior_fails = _fail_count_in_window(lk)
    _clear_login_failures(lk)

    if prior_fails >= SUSPECT_FAILS_BEFORE_SESSION_REVOKE:
        cur.execute('DELETE FROM sessions WHERE user_id = %s', (row['id'],))
        conn.commit()
        log_security_event(
            "sessions_revoked_after_bruteforce",
            message="Thu hồi session cũ sau nhiều lần đăng nhập sai rồi đăng nhập thành công",
            user_id=row['id'],
            extra={"prior_failures_in_window": prior_fails},
        )

    user = {'id': row['id'], 'email': row['email'], 'name': row['name'], 'role': row['role']}
    device_id = _request_device_id()
    access_token, refresh_token = generate_tokens(row['id'], row['email'], row['role'], device_id=device_id or None)
    save_session(row['id'], access_token, 1, device_id=device_id or None)
    save_session(row['id'], refresh_token, 168, device_id=device_id or None)

    log_security_event(
        "login_success",
        message="Đăng nhập thành công",
        user_id=row['id'],
        extra={"prior_failures_in_window": prior_fails},
    )

    cur.close()
    conn.close()

    resp = make_response(
        jsonify({
            'accessToken': access_token,
            'refreshToken': refresh_token,
            'user': user,
        })
    )
    _attach_refresh_cookie(resp, refresh_token)
    return resp


@auth_bp.route('/admin/users', methods=['GET'])
def admin_list_users():
    ok, admin_resp = _require_admin_key()
    if not ok:
        return admin_resp

    try:
        limit = int((request.args.get('limit') or '50').strip())
    except Exception:
        limit = 50
    limit = max(1, min(limit, 200))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, email, name, role, created_at FROM users ORDER BY created_at DESC LIMIT %s',
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    users = [dict(r) for r in rows]
    return jsonify({'users': users})


@auth_bp.route('/admin/login-as', methods=['POST'])
def admin_login_as():
    ok, admin_resp = _require_admin_key()
    if not ok:
        return admin_resp

    data = request.get_json(silent=True) or {}
    user_id = (data.get('userId') or data.get('user_id') or '').strip()
    email = (data.get('email') or '').strip().lower()

    if not user_id and not email:
        return jsonify({'error': 'userId hoặc email là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    if user_id:
        cur.execute(
            'SELECT id, email, name, role FROM users WHERE id = %s',
            (user_id,),
        )
    else:
        cur.execute(
            'SELECT id, email, name, role FROM users WHERE LOWER(email) = %s',
            (email,),
        )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'User không tồn tại'}), 404

    user = {'id': row['id'], 'email': row['email'], 'name': row.get('name') or '', 'role': row.get('role') or 'user'}
    device_id = _request_device_id()
    access_token, refresh_token = generate_tokens(row['id'], row['email'], user['role'], device_id=device_id or None)
    save_session(row['id'], access_token, 1, device_id=device_id or None)
    save_session(row['id'], refresh_token, 168, device_id=device_id or None)

    cur.close()
    conn.close()

    resp = make_response(
        jsonify({
            'accessToken': access_token,
            'refreshToken': refresh_token,
            'user': user,
        }),
    )
    _attach_refresh_cookie(resp, refresh_token)
    return resp


@auth_bp.route('/refresh', methods=['POST'])
def refresh():
    if not _hit_rate('refresh', 60, 300):
        return jsonify({'error': 'Quá nhiều yêu cầu, vui lòng thử lại sau'}), 429
    data = request.get_json(silent=True) or {}
    refresh_token = (data.get('refreshToken') or '').strip()
    if not refresh_token:
        refresh_token = (request.cookies.get(REFRESH_COOKIE_NAME) or '').strip()
    if not refresh_token:
        return jsonify({'error': 'Refresh token là bắt buộc'}), 400

    payload = decode_jwt(refresh_token)
    if not payload or payload.get('type') != 'refresh':
        return jsonify({'error': 'Invalid refresh token'}), 401

    token_device = (payload.get('deviceId') or '').strip()
    req_device = _request_device_id()
    if token_device and token_device != req_device:
        return jsonify({'error': 'Device mismatch'}), 401

    if not is_active_session_token(refresh_token, payload.get('userId'), token_device or None):
        return jsonify({'error': 'Refresh token invalid or revoked'}), 401

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE id = %s', (payload['userId'],))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'User not found'}), 401

    # Rotation: thu hồi refresh token cũ rồi cấp mới
    cur.execute('DELETE FROM sessions WHERE token_hash = %s', (hash_token(refresh_token),))
    conn.commit()

    # Auto-upgrade session bind:
    # - token cũ có deviceId -> giữ nguyên (đã check mismatch ở trên)
    # - token cũ chưa có deviceId -> bind theo device hiện tại từ header
    effective_device = token_device or req_device or None
    new_access, new_refresh = generate_tokens(row['id'], row['email'], row['role'], device_id=effective_device)
    save_session(row['id'], new_access, 1, device_id=effective_device)
    save_session(row['id'], new_refresh, 168, device_id=effective_device)

    resp = make_response(
        jsonify({
            'accessToken': new_access,
            'refreshToken': new_refresh,
        })
    )
    _attach_refresh_cookie(resp, new_refresh)
    return resp


@auth_bp.route('/logout', methods=['POST'])
@require_auth
def logout():
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    if token:
        conn = get_db()
        cur = conn.cursor()
        # Thu hồi toàn bộ session của user hiện tại để logout sạch trên mọi token
        cur.execute('DELETE FROM sessions WHERE user_id = %s', (request.user['id'],))
        # Giữ thêm dòng xóa theo token hiện tại để tương thích logic cũ
        cur.execute('DELETE FROM sessions WHERE token_hash = %s', (hash_token(token),))
        conn.commit()
    resp = jsonify({'message': 'Đăng xuất thành công'})
    _clear_refresh_cookie(resp)
    return resp, 200


@auth_bp.route('/revoke-device', methods=['POST'])
@require_auth
def revoke_device():
    """
    Thu hồi toàn bộ session của 1 thiết bị theo deviceId.
    Nếu không truyền deviceId trong body thì dùng header X-Device-Id.
    """
    data = request.get_json(silent=True) or {}
    device_id = (data.get('deviceId') or _request_device_id() or '').strip()
    if not device_id:
        return jsonify({'error': 'deviceId là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sessions SET revoked_at = NOW() WHERE user_id = %s AND device_id = %s AND revoked_at IS NULL",
        (request.user['id'], device_id),
    )
    affected = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Đã thu hồi session theo thiết bị', 'revoked': affected, 'deviceId': device_id})


@auth_bp.route('/me', methods=['GET'])
@require_auth
def get_me():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, email, name, role, created_at FROM users WHERE id = %s', (request.user['id'],))
    row = cur.fetchone()
    if not row:
        return jsonify({'error': 'User not found'}), 404
    return jsonify(dict(row))


@auth_bp.route('/change-password', methods=['POST'])
@require_auth
def change_password():
    if not _hit_rate('change_password', 20, 300):
        return jsonify({'error': 'Quá nhiều yêu cầu, vui lòng thử lại sau'}), 429
    """Đổi mật khẩu: yêu cầu mật khẩu cũ để xác nhận."""
    data = request.get_json(silent=True) or {}
    current_password = data.get('currentPassword', '')
    new_password = data.get('newPassword', '')

    if not current_password or not new_password:
        return jsonify({'error': 'Mật khẩu hiện tại và mật khẩu mới là bắt buộc'}), 400

    if not _is_strong_password(new_password):
        return jsonify({'error': 'Mật khẩu mới phải từ 8 ký tự, gồm chữ hoa, chữ thường và số'}), 400

    if current_password == new_password:
        return jsonify({'error': 'Mật khẩu mới phải khác mật khẩu hiện tại'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT password FROM users WHERE id = %s', (request.user['id'],))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'Không tìm thấy người dùng'}), 404

    pw_stored = row['password']
    # User đăng ký Google — không có password
    if not pw_stored:
        return jsonify({'error': 'Tài khoản này không sử dụng mật khẩu (đăng nhập qua Google)'}), 400

    if isinstance(pw_stored, str):
        pw_stored = pw_stored.encode('utf-8')
    elif isinstance(pw_stored, (bytes, bytearray, memoryview)):
        pw_stored = bytes(pw_stored)

    if not bcrypt.checkpw(current_password.encode('utf-8'), pw_stored):
        return jsonify({'error': 'Mật khẩu hiện tại không đúng'}), 401

    new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    cur.execute('UPDATE users SET password = %s WHERE id = %s', (new_hash, request.user['id']))
    # Thu hồi toàn bộ sessions để buộc đăng nhập lại sau khi đổi mật khẩu
    cur.execute('DELETE FROM sessions WHERE user_id = %s', (request.user['id'],))
    conn.commit()

    return jsonify({'message': 'Đổi mật khẩu thành công. Vui lòng đăng nhập lại.'})


# ─── Google OAuth ──────────────────────────────────────────────────────────────

def _request_origin():
    # Flask host_url luôn có dấu '/' cuối
    return request.host_url.rstrip('/')


def _frontend_url():
    # Ưu tiên FRONTEND_URL khi đã cấu hình rõ ràng
    frontend = (current_app.config.get('FRONTEND_URL') or '').strip()
    if frontend:
        return frontend.rstrip('/')

    # Fallback: cùng origin với request hiện tại (hữu ích khi chạy theo IP/domain server)
    return _request_origin()


def _google_redirect(frontend_path, use_callback_page=True):
    """Redirect về frontend với query params.

    Mặc định ưu tiên route SPA `/auth/callback` (frontend React).
    Có thể override bằng GOOGLE_CALLBACK_PATH trong .env.
    """
    base = _frontend_url()
    callback_path = (current_app.config.get('GOOGLE_CALLBACK_PATH') or '/auth/callback').strip()

    if use_callback_page:
        if not callback_path.startswith('/'):
            callback_path = '/' + callback_path
        base = base.rstrip('/') + callback_path

    separator = '&' if '?' in base else '?'
    return f'{base}{separator}{frontend_path}'


def _google_redirect_uri():
    configured = (current_app.config.get('GOOGLE_REDIRECT_URI') or '').strip()
    if configured:
        return configured

    # Fallback động theo host hiện tại của backend
    return f"{_request_origin()}/api/auth/google/callback"


@auth_bp.route('/google', methods=['GET'])
def google_auth_start():
    """
    Bước 1: Redirect user đến Google consent screen.
    Sau khi user đồng ý, Google redirect về /api/auth/google/callback với code.
    """
    client_id = current_app.config.get('GOOGLE_CLIENT_ID')
    redirect_uri = _google_redirect_uri()

    if not client_id:
        # Không có credentials → redirect về frontend với thông báo lỗi
        # (KHÔNG trả 400 JSON nữa)
        return redirect(_google_redirect('auth_error=google_not_configured'))

    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'access_type': 'offline',
        'prompt': 'consent',
    }

    google_auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)
    return redirect(google_auth_url)


@auth_bp.route('/google/callback', methods=['GET'])
def google_auth_callback():
    """
    Bước 2: Google gọi callback này với ?code=...
    Backend đổi code lấy tokens từ Google, tạo/lấy user, redirect về frontend.
    """
    code = request.args.get('code')
    error = request.args.get('error')

    if error or not code:
        return redirect(_google_redirect(f'auth_error={error or "no_code"}'))

    client_id = current_app.config.get('GOOGLE_CLIENT_ID')
    client_secret = current_app.config.get('GOOGLE_CLIENT_SECRET')
    redirect_uri = _google_redirect_uri()

    # Đổi authorization code lấy access token từ Google
    try:
        token_response = requests.post(
            'https://oauth2.googleapis.com/token',
            data={
                'code': code,
                'client_id': client_id,
                'client_secret': client_secret,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            timeout=10,
        )
        token_data = token_response.json()

        if 'access_token' not in token_data:
            return redirect(_google_redirect('auth_error=google_token_failed'))

        access_token_google = token_data['access_token']

        # Lấy thông tin user từ Google
        userinfo_response = requests.get(
            'https://www.googleapis.com/oauth2/v3/userinfo',
            headers={'Authorization': f'Bearer {access_token_google}'},
            timeout=10,
        )
        google_user = userinfo_response.json()

        google_id = google_user.get('sub', '')
        email = google_user.get('email', '').strip().lower()
        name = google_user.get('name', '') or google_user.get('given_name', '')

        if not email:
            return redirect(_google_redirect('auth_error=no_email'))

        # Tạo hoặc cập nhật user
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE google_id = %s OR email = %s', (google_id, email))
        row = cur.fetchone()

        if row:
            user_id = row['id']
            if not row['google_id']:
                cur.execute('UPDATE users SET google_id = %s, name = %s WHERE id = %s', (google_id, name, user_id))
            user = {'id': user_id, 'email': row['email'], 'name': row['name'] or name, 'role': row['role']}
        else:
            user_id = str(uuid.uuid4())
            cur.execute(
                'INSERT INTO users (id, email, password, name, google_id) VALUES (%s, %s, %s, %s, %s)',
                (user_id, email, '', name, google_id)
            )
            user = {'id': user_id, 'email': email, 'name': name, 'role': 'user'}

        conn.commit()

        # Không đưa tokens vào URL. Trả one-time ticket cho frontend đổi token qua API.
        ticket = f'otk_{uuid.uuid4().hex}'
        cur.execute(
            """
            INSERT INTO oauth_tickets (id, user_id, expires_at, used_at)
            VALUES (%s, %s, NOW() + INTERVAL '2 minutes', NULL)
            """,
            (ticket, user_id),
        )
        conn.commit()
        qs = urlencode({
            'auth': 'success',
            'ticket': ticket,
        })
        return redirect(_google_redirect(qs))

    except requests.exceptions.RequestException:
        return redirect(_google_redirect('auth_error=network_error'))


@auth_bp.route('/oauth/exchange', methods=['POST'])
def oauth_exchange():
    if not _hit_rate('oauth_exchange', 40, 300):
        return jsonify({'error': 'Quá nhiều yêu cầu, vui lòng thử lại sau'}), 429
    data = request.get_json(silent=True) or {}
    ticket = (data.get('ticket') or '').strip()
    if not ticket:
        return jsonify({'error': 'ticket là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id
        FROM oauth_tickets
        WHERE id = %s AND used_at IS NULL AND expires_at > NOW()
        """,
        (ticket,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        log_security_event(
            "oauth_ticket_invalid",
            message="Ticket OAuth không hợp lệ hoặc hết hạn",
            extra={"ticket_prefix": ticket[:12] + "..." if len(ticket) > 12 else ticket},
        )
        return jsonify({'error': 'Ticket không hợp lệ hoặc đã hết hạn'}), 401

    cur.execute("SELECT id, email, name, role FROM users WHERE id = %s", (row['user_id'],))
    user = cur.fetchone()
    if not user:
        cur.close()
        conn.close()
        log_security_event(
            "oauth_exchange_user_missing",
            message="User từ ticket không tồn tại",
            extra={"user_id": row.get("user_id")},
        )
        return jsonify({'error': 'User không tồn tại'}), 404

    cur.execute("UPDATE oauth_tickets SET used_at = NOW() WHERE id = %s", (ticket,))
    conn.commit()
    cur.close()
    conn.close()

    device_id = _request_device_id()
    access_token, refresh_token = generate_tokens(user['id'], user['email'], user['role'], device_id=device_id or None)
    save_session(user['id'], access_token, 1, device_id=device_id or None)
    save_session(user['id'], refresh_token, 168, device_id=device_id or None)
    log_security_event(
        "oauth_exchange_success",
        message="Đổi ticket OAuth thành công",
        user_id=user['id'],
    )
    resp = make_response(
        jsonify({
            'accessToken': access_token,
            'refreshToken': refresh_token,
            'user': {
                'id': user['id'],
                'email': user['email'],
                'name': user['name'],
                'role': user['role'],
            },
        })
    )
    _attach_refresh_cookie(resp, refresh_token)
    return resp

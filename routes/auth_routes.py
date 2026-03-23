from flask import Blueprint, request, jsonify, redirect, current_app
from urllib.parse import urlencode
import bcrypt
import uuid
import jwt
import requests
from db import get_db
from auth import generate_tokens, save_session, require_auth, hash_token

auth_bp = Blueprint('auth', __name__)

# ─── Email/Password Auth ───────────────────────────────────────────────────────

@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    name = data.get('name', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email và mật khẩu là bắt buộc'}), 400

    if len(password) < 6:
        return jsonify({'error': 'Mật khẩu phải có ít nhất 6 ký tự'}), 400

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
    access_token, refresh_token = generate_tokens(user_id, email, 'user')
    save_session(user_id, access_token, 1)
    save_session(user_id, refresh_token, 168)

    return jsonify({
        'accessToken': access_token,
        'refreshToken': refresh_token,
        'user': user,
    }), 201


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({'error': 'Email và mật khẩu là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE LOWER(email) = %s', (email,))
    row = cur.fetchone()

    if not row:
        print(f"[LOGIN] ❌ Không tìm thấy user: '{email}'")
        return jsonify({'error': 'Email hoặc mật khẩu không đúng'}), 401

    pw_stored = row['password']
    if isinstance(pw_stored, str):
        pw_stored = pw_stored.encode('utf-8')
    elif isinstance(pw_stored, (bytes, bytearray, memoryview)):
        pw_stored = bytes(pw_stored)

    pwd_bytes = password.encode('utf-8')
    match = bcrypt.checkpw(pwd_bytes, pw_stored)
    print(f"[LOGIN] email_client='{email}' | email_db='{row['email']}' | hash='{row['password'][:30]}...' | bcrypt_match={match}")

    if not match:
        return jsonify({'error': 'Email hoặc mật khẩu không đúng'}), 401

    user = {'id': row['id'], 'email': row['email'], 'name': row['name'], 'role': row['role']}
    access_token, refresh_token = generate_tokens(row['id'], row['email'], row['role'])
    save_session(row['id'], access_token, 1)
    save_session(row['id'], refresh_token, 168)

    return jsonify({
        'accessToken': access_token,
        'refreshToken': refresh_token,
        'user': user,
    })


@auth_bp.route('/refresh', methods=['POST'])
def refresh():
    data = request.get_json() or {}
    refresh_token = data.get('refreshToken', '')

    if not refresh_token:
        return jsonify({'error': 'Refresh token là bắt buộc'}), 400

    try:
        payload = jwt.decode(refresh_token, current_app.config['JWT_SECRET'], algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        return jsonify({'error': 'Refresh token đã hết hạn'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'error': 'Invalid refresh token'}), 401

    if payload.get('type') != 'refresh':
        return jsonify({'error': 'Invalid refresh token'}), 401

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE id = %s', (payload['userId'],))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'User not found'}), 401

    new_access, new_refresh = generate_tokens(row['id'], row['email'], row['role'])
    save_session(row['id'], new_access, 1)
    save_session(row['id'], new_refresh, 168)

    return jsonify({
        'accessToken': new_access,
        'refreshToken': new_refresh,
    })


@auth_bp.route('/logout', methods=['POST'])
@require_auth
def logout():
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    if token:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM sessions WHERE token_hash = %s', (hash_token(token),))
        conn.commit()
    return jsonify({'message': 'Đăng xuất thành công'})


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


# ─── Google OAuth ──────────────────────────────────────────────────────────────

def _frontend_url():
    return current_app.config.get('FRONTEND_URL', 'http://localhost:5173')


def _google_redirect(frontend_path, use_callback_page=True):
    """Redirect về frontend với query params.

    Khi use_callback_page=True (mặc định), redirect sang trang riêng
    /pages/auth-callback.html để xử lý auth — tránh race condition với includeHtml.
    """
    base = _frontend_url()
    if use_callback_page:
        base = base.rstrip('/') + '/pages/auth-callback.html'
    separator = '&' if '?' in base else '?'
    return f'{base}{separator}{frontend_path}'


@auth_bp.route('/google', methods=['GET'])
def google_auth_start():
    """
    Bước 1: Redirect user đến Google consent screen.
    Sau khi user đồng ý, Google redirect về /api/auth/google/callback với code.
    """
    client_id = current_app.config.get('GOOGLE_CLIENT_ID')
    redirect_uri = current_app.config.get('GOOGLE_REDIRECT_URI', 'http://localhost:3001/api/auth/google/callback')

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
    redirect_uri = current_app.config.get('GOOGLE_REDIRECT_URI', 'http://localhost:3001/api/auth/google/callback')

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

        # Tạo JWT tokens
        access_token, refresh_token = generate_tokens(user_id, email, user['role'])
        save_session(user_id, access_token, 1)
        save_session(user_id, refresh_token, 168)

        # Redirect về frontend với tokens (URL-encode để tránh lỗi)
        qs = urlencode({
            'auth': 'success',
            'accessToken': access_token,
            'refreshToken': refresh_token,
            'userId': user_id,
            'userEmail': email,
            'userName': name,
        })
        return redirect(_google_redirect(qs))

    except requests.exceptions.RequestException as e:
        print(f'Google OAuth error: {e}')
        return redirect(_google_redirect('auth_error=network_error'))

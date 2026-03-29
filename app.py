from flask import Flask, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from os import environ
from pathlib import Path

# Load .env
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    # Ưu tiên giá trị trong file .env của project, tránh bị biến môi trường hệ thống ghi đè
    load_dotenv(env_path, override=True)

from db import init_db
from routes.auth_routes import auth_bp
from routes.products import products_bp
from routes.orders import orders_bp
from routes.payments import payments_bp
from routes.admin import admin_bp
from routes.wallet import wallet_bp
from routes.secure import secure_bp
from routes.license import license_bp
from routes.validate import validate_bp
from routes.me import me_bp
from routes.desktop_public import desktop_public_bp, desktop_legacy_bp

app = Flask(__name__)
app.config['SECRET_KEY'] = environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['JWT_SECRET'] = environ.get('JWT_SECRET', environ.get('SECRET_KEY', 'dev-jwt-key'))
# Secure API key cho mã hóa 2 chiều (Electron <-> Backend)
app.config['SECURE_API_KEY'] = environ.get('SECURE_API_KEY', 'secure-api-key-change-in-production-32chars')

# Google OAuth config
app.config['GOOGLE_CLIENT_ID'] = environ.get('GOOGLE_CLIENT_ID', '')
app.config['GOOGLE_CLIENT_SECRET'] = environ.get('GOOGLE_CLIENT_SECRET', '')
# Khuyến nghị set trực tiếp trong .env khi deploy qua domain/IP public
app.config['GOOGLE_REDIRECT_URI'] = environ.get('GOOGLE_REDIRECT_URI', '')
# Frontend public URL để redirect sau khi OAuth thành công
app.config['FRONTEND_URL'] = environ.get('FRONTEND_URL', '')
# OAuth callback path trên frontend (mặc định /auth/callback)
app.config['GOOGLE_CALLBACK_PATH'] = environ.get('GOOGLE_CALLBACK_PATH', '/auth/callback')

# MinIO config
app.config['MINIO_ENDPOINT'] = environ.get('MINIO_ENDPOINT', 'minio01.vpsmmo.vn:9000')
app.config['MINIO_ACCESS_KEY'] = environ.get('MINIO_ACCESS_KEY', 'nthieu228')
app.config['MINIO_SECRET_KEY'] = environ.get('MINIO_SECRET_KEY', '')
app.config['MINIO_BUCKET'] = environ.get('MINIO_BUCKET', 'bucket-nthieu228')
app.config['MINIO_IMAGE_FOLDER'] = environ.get('MINIO_IMAGE_FOLDER', 'hinh_anh_dong-store')

# Firebase Realtime DB (Douyin License Server — tạo key khi bán hàng)
app.config['FIREBASE_URL'] = environ.get('FIREBASE_URL', '')
app.config['FIREBASE_AUTH'] = environ.get('FIREBASE_AUTH', '')

# License Validation (port từ Douyin server)
# FERNET_KEY: Fernet symmetric key trả về client khi validate OK
app.config['FERNET_KEY'] = environ.get('FERNET_KEY', '')
# Ed25519 private key (base64 của 32 bytes) — ký response validate
app.config['SIGNING_PRIVATE_KEY_B64'] = environ.get('SIGNING_PRIVATE_KEY_B64', '')
# Admin key cho Douyin-style admin endpoints (X-Admin-Key header)
app.config['ADMIN_KEY'] = environ.get('ADMIN_KEY', '')

# Banking / VietQR config
app.config['BANK_ID'] = environ.get('BANK_ID', '970422')
app.config['BANK_ACCOUNT_NUMBER'] = environ.get('BANK_ACCOUNT_NUMBER', '')
app.config['BANK_ACCOUNT_NAME'] = environ.get('BANK_ACCOUNT_NAME', '')
# VietQR Quick Link
app.config['VIETQR_TEMPLATE'] = environ.get('VIETQR_TEMPLATE', 'UH8kzQZ')

# Thueapibank.vn auto-check
app.config['THUEAPIBANK_PASSWORD'] = environ.get('THUEAPIBANK_PASSWORD', '')
app.config['THUEAPIBANK_TOKEN'] = environ.get('THUEAPIBANK_TOKEN', '')
app.config['THUEAPIBANK_API_BASE'] = environ.get('THUEAPIBANK_API_BASE', 'https://thueapibank.vn')

# OneDrive / Microsoft Graph (âm thanh — sounds). Bật khi đủ biến môi trường.
app.config['MS_GRAPH_TENANT_ID'] = environ.get('MS_GRAPH_TENANT_ID', '').strip()
app.config['MS_GRAPH_CLIENT_ID'] = environ.get('MS_GRAPH_CLIENT_ID', '').strip()
app.config['MS_GRAPH_CLIENT_SECRET'] = environ.get('MS_GRAPH_CLIENT_SECRET', '').strip()
app.config['MS_GRAPH_DRIVE_USER'] = environ.get('MS_GRAPH_DRIVE_USER', '').strip()
app.config['ONEDRIVE_SOUNDS_FOLDER'] = environ.get('ONEDRIVE_SOUNDS_FOLDER', 'DongStore').strip().strip('/')
# File cài app (Graph): mặc định DongStore_sounds/phanmem — tự tạo folder khi upload
app.config['ONEDRIVE_APP_INSTALLER_FOLDER'] = environ.get(
    'ONEDRIVE_APP_INSTALLER_FOLDER', 'DongStore_sounds/phanmem'
).strip().strip('/')
app.config['API_BASE_URL'] = environ.get('API_BASE_URL', '').rstrip('/')

# CORS: nếu chỉ set FRONTEND_URL trong .env mà KHÔNG set CORS_ORIGINS, trước đây chỉ cho phép
# đúng 1 origin (vd. 5173) → Vite chạy 5177 sẽ bị Failed to fetch. Giờ luôn gộp default + FRONTEND_URL.
_default_cors = (
    'http://localhost:5173,http://localhost:5174,http://localhost:5175,'
    'http://localhost:5176,http://localhost:5177,http://localhost:5178,'
    'http://localhost:5179,http://localhost:5180,'
    'http://127.0.0.1:5173,http://127.0.0.1:5174,http://127.0.0.1:5175,'
    'http://127.0.0.1:5176,http://127.0.0.1:5177,http://127.0.0.1:5178,'
    'http://127.0.0.1:5179,http://127.0.0.1:5180'
)


def _build_cors_origins():
    explicit = environ.get('CORS_ORIGINS', '').strip()
    if explicit:
        return [o.strip() for o in explicit.split(',') if o.strip()]
    merged = _default_cors
    fu = environ.get('FRONTEND_URL', '').strip()
    if fu:
        parts = [p.strip() for p in merged.split(',') if p.strip()]
        if fu not in parts:
            parts.append(fu)
        return parts
    return [o.strip() for o in merged.split(',') if o.strip()]


_cors_list = _build_cors_origins()
CORS(
    app,
    origins=_cors_list,
    supports_credentials=True,
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
    allow_headers=['Content-Type', 'Authorization', 'X-Admin-Key'],
)


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    # Chỉ bật HSTS khi chạy HTTPS thật
    if request_is_secure():
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


def request_is_secure() -> bool:
    # Hỗ trợ reverse proxy gửi X-Forwarded-Proto
    if environ.get('FORCE_HTTPS', '').strip() == '1':
        return True
    return environ.get('PREFERRED_URL_SCHEME', '').strip().lower() == 'https'


# Init database
init_db()

# Background job: hủy đơn pending quá 30 phút mỗi 5 phút
from routes.orders import cancel_expired_pending_orders
from routes.wallet import create_auto_check_job
from apscheduler.schedulers.background import BackgroundScheduler

_scheduler = BackgroundScheduler()
_scheduler.add_job(cancel_expired_pending_orders, 'interval', minutes=5, id='cancel_expired_orders')
_scheduler.add_job(create_auto_check_job(app), 'interval', seconds=5, id='check_pending_deposits')
_scheduler.start()
print("Background scheduler started")

# Set bucket public để ảnh load trực tiếp (không qua proxy)
try:
    from upload import ensure_bucket_public
    ensure_bucket_public()
except Exception as e:
    print(f"Không set được bucket public: {e}")

# Routes
app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(products_bp, url_prefix='/api/products')
app.register_blueprint(orders_bp, url_prefix='/api/orders')
app.register_blueprint(payments_bp, url_prefix='/api/payments')
app.register_blueprint(admin_bp, url_prefix='/api/admin')
app.register_blueprint(wallet_bp, url_prefix='/api/wallet')
app.register_blueprint(secure_bp, url_prefix='/api/secure')
app.register_blueprint(license_bp, url_prefix='/api/license')
app.register_blueprint(validate_bp, url_prefix='/api')
app.register_blueprint(me_bp, url_prefix='/api/me')
app.register_blueprint(desktop_public_bp, url_prefix='/api/desktop-updates')
app.register_blueprint(desktop_legacy_bp, url_prefix='/api/app-desktop')


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'DONG STORE API',
        'google_configured': bool(app.config.get('GOOGLE_CLIENT_ID')),
        'timestamp': __import__('datetime').datetime.utcnow().isoformat()
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Internal server error'}), 500


if __name__ == '__main__':
    port = int(environ.get('PORT', 3001))
    debug = environ.get('FLASK_ENV', 'development') == 'development'
    google_ok = bool(environ.get('GOOGLE_CLIENT_ID'))

    if not app.config['JWT_SECRET'] or app.config['JWT_SECRET'] in ('dev-jwt-key', 'dev-secret-key-change-in-production'):
        print('⚠️  Cảnh báo: JWT_SECRET/SECRET_KEY đang dùng giá trị mặc định. Hãy đổi ngay trong .env khi deploy.')

    print(f"DONG STORE API running on http://localhost:{port}")
    print(f"Debug: {debug}")
    print("Database: PostgreSQL")
    print(f"Google OAuth: {'Configured' if google_ok else 'Not configured'}")
    print(f"FRONTEND_URL: {environ.get('FRONTEND_URL', 'http://localhost:5173')}")
    print(f"CORS: {len(_cors_list)} origin(s)")
    app.run(host='0.0.0.0', port=port, debug=debug)

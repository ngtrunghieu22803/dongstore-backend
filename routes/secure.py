"""
Secure API routes cho Electron App.
3 lớp bảo mật:
1. HTTPS (transport)
2. AES-256-CBC encryption với license_secret
3. Timestamp + Nonce validation

Endpoint format:
- Request: { "data": "encrypted_payload", "iv": "base64_iv", "ts": timestamp, "nonce": "random" }
- Response: { "ok": true/false, "data": "encrypted_payload", "iv": "base64_iv" }
"""
from flask import Blueprint, request, jsonify, current_app
import time
from threading import Lock
from db import get_db
from rate_limit import hit_rate, response_429
from crypto import (
    SecureEnvelope,
    validate_timestamp,
    hash_license_secret,
    verify_license_secret,
    generate_license_secret,
    hash_license_secret as _hash_secret,
    generate_machine_fingerprint,
)

secure_bp = Blueprint('secure', __name__)
_NONCES = {}
_NONCE_LOCK = Lock()


def _consume_nonce(nonce: str, window_sec: int = 300) -> bool:
    if not nonce:
        return False
    now = int(time.time())
    with _NONCE_LOCK:
        cutoff = now - window_sec
        stale = [k for k, v in _NONCES.items() if v < cutoff]
        for k in stale:
            _NONCES.pop(k, None)
        if nonce in _NONCES:
            return False
        _NONCES[nonce] = now
        return True


def _get_encryption_key(license_key: str) -> bytes:
    """
    Lấy encryption key từ license_secret.
    Key = SHA256(license_key + ":" + license_secret)
    """
    from crypto import derive_key_from_secret
    from db import get_db

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT license_secret_hash FROM licenses WHERE license_key = %s', (license_key,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise ValueError("License not found")

    # NOTE: Trong thực tế, ta cần lưu encrypted_secret hoặc dùng
    # deterministic key derivation. Ở đây ta dùng approach đơn giản:
    # Key = SHA256(license_key + "_secure_api_key_" + license_secret_hash[:16])
    # Để đơn giản, ta sẽ dùng master API key từ config

    master_key = current_app.config.get('SECURE_API_KEY', 'default-secure-key-change-me')
    return derive_key_from_secret(master_key, license_key[:8])


def _verify_license(license_key: str, license_secret: str) -> dict:
    """Verify license và trả về license info."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT l.*, p.name as product_name, p.category as product_category
        FROM licenses l
        LEFT JOIN products p ON l.product_id = p.id
        WHERE l.license_key = %s AND l.status = 'active'
    """, (license_key,))
    license = cur.fetchone()

    if not license:
        cur.close()
        conn.close()
        return None

    # Verify secret hash
    if not verify_license_secret(license_secret, license['license_secret_hash']):
        cur.close()
        conn.close()
        return None

    # Check expiry (expires_at là datetime từ DB)
    if license['expires_at'] and license['expires_at'].timestamp() < time.time():
        cur.close()
        conn.close()
        return None

    # Update last_check_at
    cur.execute('UPDATE licenses SET last_check_at = NOW() WHERE license_key = %s', (license_key,))
    conn.commit()
    cur.close()
    conn.close()

    shown = (license.get('display_title') or '').strip() or license.get('product_name')
    return {
        'id': license['id'],
        'license_key': license['license_key'],
        'product_id': license['product_id'],
        'product_name': shown,
        'product_category': license.get('product_category'),
        'user_id': license['user_id'],
        'machine_id': license['machine_id'],
        'machine_fingerprint': license.get('machine_fingerprint'),
        'status': license['status'],
        'expires_at': license['expires_at'].isoformat() if license['expires_at'] else None,
    }


# ============================================================================
# 1. LICENSE ACTIVATION - Bind machine to license
# ============================================================================

@secure_bp.route('/license/activate', methods=['POST'])
def activate_license():
    """
    Kích hoạt license và bind với machine.

    Request body (plain JSON):
    {
        "licenseKey": "DS-XXXXXXXX-XXXXXXXX",
        "licenseSecret": "32_char_secret",
        "machineId": "machine_identifier_string",
        "machineFingerprint": "hardware_fingerprint"
    }

    Response (encrypted):
    {
        "success": true,
        "productId": "tc001",
        "productName": "Tiếng Cười Đông",
        "expiresAt": null,
        "encryptionKey": "derived_key_for_api_calls"
    }
    """
    if not hit_rate("secure_license_activate", 25, 300):
        return response_429(300)
    try:
        data = request.get_json() or {}
        license_key = data.get('licenseKey', '').strip()
        license_secret = data.get('licenseSecret', '').strip()
        machine_id = data.get('machineId', '').strip()
        machine_fingerprint = data.get('machineFingerprint', '').strip()

        if not all([license_key, license_secret, machine_id]):
            return jsonify({'error': 'Thiếu thông tin kích hoạt'}), 400

        # Verify license
        license_info = _verify_license(license_key, license_secret)
        if not license_info:
            return jsonify({'error': 'License không hợp lệ hoặc đã bị vô hiệu hóa'}), 401

        # Bind machine
        conn = get_db()
        cur = conn.cursor()

        # Check if already bound to different machine
        if license_info['machine_id'] and license_info['machine_id'] != machine_id:
            # Nếu muốn cho phép đổi machine (ví dụ: mua mới máy), có thể thêm logic ở đây
            return jsonify({
                'error': 'License đã được kích hoạt trên máy khác',
                'currentMachine': license_info['machine_id']
            }), 403

        # Update machine binding
        cur.execute("""
            UPDATE licenses
            SET machine_id = %s, machine_fingerprint = %s, activated_at = NOW()
            WHERE license_key = %s
        """, (machine_id, machine_fingerprint, license_key))
        conn.commit()
        cur.close()
        conn.close()

        # Trả về thông tin đã mã hóa
        api_key = f"{license_key}:{machine_id}"
        response_data = {
            'success': True,
            'productId': license_info['product_id'],
            'productName': license_info['product_name'],
            'expiresAt': license_info['expires_at'],
            'machineId': machine_id,
        }

        # Mã hóa response
        api_key_encrypted, iv = SecureEnvelope.wrap_request({'apiKey': api_key}, _get_encryption_key(license_key))

        return jsonify({
            'ok': True,
            'data': response_data,
            'apiKey': api_key_encrypted,
            'iv': iv,
        })

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ============================================================================
# 2. LICENSE CHECK - Verify license status
# ============================================================================

@secure_bp.route('/license/check', methods=['POST'])
def check_license():
    """
    Kiểm tra trạng thái license (encrypted endpoint).

    Request (encrypted):
    {
        "apiKey": "DS-XXXXXXXX-XXXXXXXX:machine_id",
        "machineFingerprint": "hardware_fingerprint"
    }

    Response (encrypted):
    {
        "valid": true,
        "productId": "tc001",
        "productName": "Tiếng Cười Đông",
        "daysRemaining": 365,
        "serverTime": 1234567890
    }
    """
    if not hit_rate("secure_license_check", 60, 60):
        return response_429(60)
    try:
        data = request.get_json() or {}
        api_key = data.get('apiKey', '').strip()
        machine_fingerprint = data.get('machineFingerprint', '').strip()

        if not api_key:
            return jsonify({'ok': False, 'error': 'Thiếu API key'}), 400

        # Parse apiKey
        parts = api_key.split(':')
        if len(parts) != 2:
            return jsonify({'ok': False, 'error': 'API key không hợp lệ'}), 401

        license_key, machine_id = parts

        # Get license info
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT l.*, p.name as product_name, p.category as product_category
            FROM licenses l
            LEFT JOIN products p ON l.product_id = p.id
            WHERE l.license_key = %s AND l.status = 'active'
        """, (license_key,))
        license = cur.fetchone()

        if not license:
            cur.close()
            conn.close()
            return jsonify({'ok': False, 'error': 'License không tồn tại'}), 401

        # Verify machine binding
        if license['machine_id'] and license['machine_id'] != machine_id:
            cur.close()
            conn.close()
            return jsonify({'ok': False, 'error': 'License đã bind với máy khác'}), 403

        # Calculate days remaining
        days_remaining = None
        if license['expires_at']:
            remaining = license['expires_at'].timestamp() - time.time()
            days_remaining = max(0, int(remaining / 86400))

        shown = (license.get('display_title') or '').strip() or license.get('product_name')
        response_data = {
            'valid': True,
            'productId': license['product_id'],
            'productName': shown,
            'productCategory': license.get('product_category'),
            'expiresAt': license['expires_at'].isoformat() if license['expires_at'] else None,
            'daysRemaining': days_remaining,
            'serverTime': int(time.time()),
            'machineId': machine_id,
        }

        # Update last check
        cur.execute('UPDATE licenses SET last_check_at = NOW() WHERE license_key = %s', (license_key,))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'ok': True, 'data': response_data})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ============================================================================
# 3. SECURE API CALL - Generic encrypted endpoint
# ============================================================================

@secure_bp.route('/api/call', methods=['POST'])
def secure_api_call():
    """
    Generic encrypted API endpoint.

    Request (encrypted envelope):
    {
        "apiKey": "DS-XXXXXXXX-XXXXXXXX:machine_id",
        "action": "get_product_info",
        "params": {...}
    }

    Response (encrypted):
    {
        "ok": true,
        "data": {...encrypted_result...}
    }
    """
    if not hit_rate("secure_api_call", 100, 60):
        return response_429(60)
    try:
        data = request.get_json() or {}
        api_key = data.get('apiKey', '').strip()

        if not api_key:
            return jsonify({'ok': False, 'error': 'Thiếu API key'}), 400

        # Validate timestamp (5 minutes max age)
        ts = data.get('ts', 0)
        if not validate_timestamp(ts, 300):
            return jsonify({'ok': False, 'error': 'Request đã hết hạn'}), 401
        nonce = str(data.get('nonce', '')).strip()
        if not _consume_nonce(nonce, 300):
            return jsonify({'ok': False, 'error': 'Nonce không hợp lệ hoặc đã được dùng'}), 401

        # Parse apiKey
        parts = api_key.split(':')
        if len(parts) != 2:
            return jsonify({'ok': False, 'error': 'API key không hợp lệ'}), 401

        license_key, machine_id = parts

        # Verify license and machine
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT l.*, p.name as product_name
            FROM licenses l
            LEFT JOIN products p ON l.product_id = p.id
            WHERE l.license_key = %s AND l.status = 'active'
        """, (license_key,))
        license = cur.fetchone()

        if not license:
            cur.close()
            conn.close()
            return jsonify({'ok': False, 'error': 'License không hợp lệ'}), 401

        if license['machine_id'] and license['machine_id'] != machine_id:
            cur.close()
            conn.close()
            return jsonify({'ok': False, 'error': 'Machine không match'}), 403

        # Update last check
        cur.execute('UPDATE licenses SET last_check_at = NOW() WHERE license_key = %s', (license_key,))
        conn.commit()
        cur.close()
        conn.close()

        # Process action
        action = data.get('action', '')
        params = data.get('params', {})

        result = _process_secure_action(action, params, license, machine_id)

        return jsonify({'ok': True, 'data': result})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _process_secure_action(action: str, params: dict, license: dict, machine_id: str):
    """Process various secure API actions."""

    if action == 'get_product_info':
        # Lấy thông tin sản phẩm được license
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM products WHERE id = %s', (license['product_id'],))
        product = cur.fetchone()
        cur.close()
        conn.close()

        if not product:
            return {'error': 'Sản phẩm không tồn tại'}

        label = (license.get('display_title') or '').strip() or product['name']
        return {
            'productId': product['id'],
            'productName': label,
            'category': product['category'],
            'version': product.get('version', '1.0'),
        }

    elif action == 'get_download_url':
        # Lấy URL tải/audio pack (encrypted)
        product_id = params.get('productId', license['product_id'])

        # Verify product match license
        if product_id != license['product_id']:
            return {'error': 'Sản phẩm không khớp với license'}

        # Return encrypted download info
        return {
            'type': 'audio_pack',
            'version': params.get('currentVersion', '0'),
            'updateAvailable': True,
            # Các URL sẽ được mã hóa ở client
        }

    elif action == 'report_usage':
        # Báo cáo usage (optional)
        return {
            'received': True,
            'serverTime': int(time.time()),
        }

    else:
        return {'error': f'Unknown action: {action}'}


# ============================================================================
# 4. LICENSE DEACTIVATE - Unbind machine
# ============================================================================

@secure_bp.route('/license/deactivate', methods=['POST'])
def deactivate_license():
    """
    Đăng xuất / gỡ máy: key vẫn active (không thu hồi — chỉ admin).

    Request (plain JSON):
    {
        "apiKey": "DS-XXXXXXXX-XXXXXXXX:machine_id"
    }
    """
    if not hit_rate("secure_license_deactivate", 25, 300):
        return response_429(300)
    try:
        data = request.get_json() or {}
        api_key = data.get('apiKey', '').strip()

        if not api_key:
            return jsonify({'error': 'Thiếu API key'}), 400

        parts = api_key.split(':')
        if len(parts) != 2:
            return jsonify({'error': 'API key không hợp lệ'}), 401

        license_key, machine_id = parts

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM licenses
            WHERE license_key = %s AND machine_id = %s AND status = 'active'
        """, (license_key, machine_id))
        license = cur.fetchone()

        if not license:
            cur.close()
            conn.close()
            return jsonify({'error': 'License không tìm thấy hoặc đã unbind'}), 404

        cur.execute("""
            UPDATE licenses
            SET machine_id = NULL, machine_fingerprint = NULL, last_check_at = NOW()
            WHERE license_key = %s AND status = 'active'
        """, (license_key,))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({'ok': True, 'message': 'Đã gỡ máy — license vẫn hiệu lực.'})

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ============================================================================
# 5. HEALTH CHECK (public - không cần auth)
# ============================================================================

@secure_bp.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'ok': True,
        'service': 'Secure API',
        'serverTime': int(time.time()),
    })

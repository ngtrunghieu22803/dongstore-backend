"""
License API routes cho Electron App.
- POST /api/license/activate — Kích hoạt + bind máy (body: form `_` = blob AES, bên trong JSON)
- POST /api/license/check — Kiểm tra license (cùng định dạng `_`)
- POST /api/license/deactivate — Stub tương thích (200, không đổi DB); app mới không gọi.
"""
from flask import Blueprint, request, jsonify, current_app, has_request_context
import json
import os
import time
import secrets
from db import get_db
from crypto import derive_key_from_secret, decrypt_aes_256_cbc
from urllib.parse import parse_qs

license_bp = Blueprint('license', __name__)

LICENSE_TRANSPORT_SALT = os.environ.get('LICENSE_TRANSPORT_SALT', 'tc-dong-v1')
LICENSE_PAYLOAD_MAX_AGE_SEC = int(os.environ.get('LICENSE_PAYLOAD_MAX_AGE_SEC', '300'))


def _license_transport_key_bytes() -> bytes:
    if has_request_context():
        secret = (current_app.config.get('SECURE_API_KEY') or '').strip()
    else:
        secret = ''
    if not secret:
        secret = os.environ.get('SECURE_API_KEY', 'secure-api-key-change-in-production-32chars').strip()
    return derive_key_from_secret(secret, LICENSE_TRANSPORT_SALT)


def _get_transport_blob() -> str:
    """Lấy chuỗi `_` từ form hoặc parse thủ công raw body (proxy / client gửi lệch Content-Type)."""
    b = (request.form.get('_') or '').strip()
    if b:
        return b
    raw = request.get_data(cache=True, as_text=False) or b''
    if not raw:
        return ''
    ct = (request.content_type or '').lower()
    if 'application/json' in ct and raw[:1] in (b'{', b'['):
        return ''
    is_form = 'application/x-www-form-urlencoded' in ct
    if not is_form and b'_=' not in raw[:16384]:
        return ''
    try:
        text = raw.decode('utf-8')
    except Exception:
        text = raw.decode('latin-1', errors='replace')
    pairs = parse_qs(text, keep_blank_values=True, strict_parsing=False)
    vals = pairs.get('_')
    if not vals:
        return ''
    return (vals[0] or '').strip()


def _parse_obfuscated_license_body() -> dict:
    blob = _get_transport_blob()
    if not blob:
        raise ValueError('Thiếu payload')
    try:
        plaintext = decrypt_aes_256_cbc(blob, _license_transport_key_bytes(), iv_b64=None)
        data = json.loads(plaintext)
    except Exception:
        raise ValueError('Payload không hợp lệ')
    ts = data.get('ts')
    if ts is None:
        raise ValueError('Thiếu ts')
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        raise ValueError('ts không hợp lệ')
    if abs(int(time.time()) - ts_int) > LICENSE_PAYLOAD_MAX_AGE_SEC:
        raise ValueError('Payload hết hạn hoặc lệch giờ')
    return data


def _get_product_prefix(product_id: str) -> str:
    """Lấy prefix cho license key dựa trên product_id."""
    prefixes = {
        'tc001': 'TCDS',
    }
    return prefixes.get(product_id, 'DS')


def _generate_license_key(product_id: str) -> str:
    """Tạo license key với prefix phù hợp."""
    uid = secrets.token_hex(8).upper()
    prefix = _get_product_prefix(product_id)
    return f'{prefix}-{uid[:8]}-{uid[8:16]}'


# ============================================================================
# LICENSE ACTIVATE - Kích hoạt license và bind với machine
# ============================================================================

@license_bp.route('/activate', methods=['POST'])
def activate_license():
    """
    Kích hoạt license và bind với machine.

    Request: application/x-www-form-urlencoded, `_` = base64(iv||ciphertext AES-256-CBC).
    Plaintext JSON: licenseKey, machineId, machineFingerprint, ts, n

    Response:
    {
        "success": true,
        "productName": "Tiếng Cười Đông",
        "expiresAt": null
    }
    """
    try:
        try:
            data = _parse_obfuscated_license_body()
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        license_key = data.get('licenseKey', '').strip()
        machine_id = data.get('machineId', '').strip()
        machine_fingerprint = data.get('machineFingerprint', '').strip()

        if not license_key:
            return jsonify({'success': False, 'error': 'Thiếu license key'}), 400

        if not machine_id:
            return jsonify({'success': False, 'error': 'Thiếu machine ID'}), 400

        conn = get_db()
        cur = conn.cursor()

        # Lấy license info
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
            return jsonify({'success': False, 'error': 'License không tồn tại hoặc đã bị vô hiệu hóa'}), 401

        # Check expiry (expires_at là datetime từ DB, không so sánh trực tiếp với float)
        if license['expires_at'] and license['expires_at'].timestamp() < time.time():
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'License đã hết hạn'}), 401

        # Check if already bound to different machine
        if license['machine_id'] and license['machine_id'] != machine_id:
            cur.close()
            conn.close()
            return jsonify({
                'success': False,
                'error': 'License đã được kích hoạt trên máy khác',
                'currentMachine': license['machine_id']
            }), 403

        # Bind machine
        cur.execute("""
            UPDATE licenses
            SET machine_id = %s, machine_fingerprint = %s, activated_at = NOW(), last_check_at = NOW()
            WHERE license_key = %s
        """, (machine_id, machine_fingerprint, license_key))
        conn.commit()

        # Tính days remaining
        days_remaining = None
        expires_at_iso = None
        if license['expires_at']:
            remaining = license['expires_at'].timestamp() - time.time()
            days_remaining = max(0, int(remaining / 86400))
            expires_at_iso = license['expires_at'].isoformat()

        cur.close()
        conn.close()

        shown = (license.get('display_title') or '').strip() or license.get('product_name')
        return jsonify({
            'success': True,
            'productId': license['product_id'],
            'productName': shown,
            'expiresAt': expires_at_iso,
            'daysRemaining': days_remaining,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# LICENSE CHECK - Kiểm tra trạng thái license
# ============================================================================

@license_bp.route('/check', methods=['POST'])
def check_license():
    """
    Kiểm tra trạng thái license.

    Request: cùng định dạng form `_` như /activate.

    Response:
    {
        "valid": true,
        "productId": "tc001",
        "productName": "Tiếng Cười Đông",
        "daysRemaining": 365,
        "expiresAt": "2027-03-26T00:00:00"
    }
    """
    try:
        try:
            data = _parse_obfuscated_license_body()
        except ValueError as e:
            return jsonify({'valid': False, 'error': str(e)}), 400
        license_key = data.get('licenseKey', '').strip()
        machine_id = data.get('machineId', '').strip()
        machine_fingerprint = data.get('machineFingerprint', '').strip()

        if not license_key:
            return jsonify({'error': 'Thiếu license key'}), 400

        conn = get_db()
        cur = conn.cursor()

        # Lấy license info
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
            return jsonify({'valid': False, 'error': 'License không tồn tại'}), 401

        # Verify machine binding
        if license['machine_id'] and license['machine_id'] != machine_id:
            cur.close()
            conn.close()
            return jsonify({'valid': False, 'error': 'License đã bind với máy khác'}), 403

        # Check expiry
        valid = True
        if license['expires_at'] and license['expires_at'].timestamp() < time.time():
            valid = False

        # Calculate days remaining
        days_remaining = None
        expires_at_iso = None
        if license['expires_at']:
            remaining = license['expires_at'].timestamp() - time.time()
            days_remaining = max(0, int(remaining / 86400))
            expires_at_iso = license['expires_at'].isoformat()

        # Update last check
        cur.execute('UPDATE licenses SET last_check_at = NOW() WHERE license_key = %s', (license_key,))
        conn.commit()
        cur.close()
        conn.close()

        shown = (license.get('display_title') or '').strip() or license.get('product_name')
        return jsonify({
            'valid': valid,
            'productId': license['product_id'],
            'productName': shown,
            'productCategory': license.get('product_category'),
            'expiresAt': expires_at_iso,
            'daysRemaining': days_remaining,
            'serverTime': int(time.time()),
        })

    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)}), 500


# ============================================================================
# DEACTIVATE — Stub (client cũ / bản build cũ vẫn có thể gọi; tránh 404)
# App Tiếng Cười Động hiện chỉ xóa license cục bộ, không cần phản hồi DB.
# ============================================================================

@license_bp.route('/deactivate', methods=['POST'])
def deactivate_license_stub():
    """Trả 200, không chỉnh DB — tránh lỗi «Endpoint not found» cho client cũ."""
    return jsonify({
        'success': True,
        'message': 'OK — đăng xuất thực hiện cục bộ trên app, server không gỡ máy.',
    })

"""
License API routes cho Electron App.
- POST /api/license/activate — Kích hoạt + bind máy (JSON/form thường)
- POST /api/license/check — Kiểm tra license (JSON/form thường)
- POST /api/license/deactivate — Stub tương thích (200, không đổi DB); app mới không gọi.
"""
from flask import Blueprint, request, jsonify, current_app
import base64
import json
import time
import secrets
from db import get_db
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

license_bp = Blueprint('license', __name__)

def _get_signing_key() -> Optional[Ed25519PrivateKey]:
    b64 = (current_app.config.get("SIGNING_PRIVATE_KEY_B64") or "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        if len(raw) != 32:
            return None
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        return None


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _signed_response(payload: dict, status_code: int = 200):
    key = _get_signing_key()
    if key is None:
        # Fallback: không ký được thì trả plain (tránh làm app chết hoàn toàn).
        return jsonify(payload), status_code
    sig = key.sign(_canonical_json_bytes(payload))
    return jsonify({
        "payload": payload,
        "sig": base64.b64encode(sig).decode("ascii"),
    }), status_code


def _respond(payload: dict, status_code: int = 200):
    # Luồng license của app này trả signed response thống nhất (không còn nhánh client cũ).
    # Trả HTTP 200 để client luôn parse body và verify chữ ký;
    # mã trạng thái logic được nhúng trong payload._http.
    signed_payload = dict(payload)
    signed_payload.setdefault("_http", status_code)
    return _signed_response(signed_payload, 200)


def _parse_license_request_body() -> dict:
    """
    Chỉ nhận JSON hoặc form thường chứa trực tiếp
    licenseKey/machineId/machineFingerprint.
    """
    data = {}

    # JSON body
    if request.is_json:
        data = request.get_json(silent=True) or {}

    # form body thường
    if not data and request.form:
        data = {
            'licenseKey': (request.form.get('licenseKey') or '').strip(),
            'machineId': (request.form.get('machineId') or '').strip(),
            'machineFingerprint': (request.form.get('machineFingerprint') or '').strip(),
        }

    if not isinstance(data, dict) or not data:
        raise ValueError('Thiếu payload')

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

    Request: JSON/form thường: licenseKey, machineId, machineFingerprint

    Response:
    {
        "success": true,
        "productName": "Tiếng Cười Đông",
        "expiresAt": null
    }
    """
    try:
        try:
            data = _parse_license_request_body()
        except ValueError as e:
            return _respond({'success': False, 'error': str(e)}, 400)
        license_key = data.get('licenseKey', '').strip()
        machine_id = data.get('machineId', '').strip()
        machine_fingerprint = data.get('machineFingerprint', '').strip()

        if not license_key:
            return _respond({'success': False, 'error': 'Thiếu license key'}, 400)

        if not machine_id:
            return _respond({'success': False, 'error': 'Thiếu machine ID'}, 400)

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
            return _respond({'success': False, 'error': 'License không tồn tại hoặc đã bị vô hiệu hóa'}, 401)

        # Check expiry (expires_at là datetime từ DB, không so sánh trực tiếp với float)
        if license['expires_at'] and license['expires_at'].timestamp() < time.time():
            cur.close()
            conn.close()
            return _respond({'success': False, 'error': 'License đã hết hạn'}, 401)

        # Check if already bound to different machine
        if license['machine_id'] and license['machine_id'] != machine_id:
            cur.close()
            conn.close()
            return _respond({
                'success': False,
                'error': 'License đã được kích hoạt trên máy khác',
                'currentMachine': license['machine_id']
            }, 403)

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
        return _respond({
            'success': True,
            'productId': license['product_id'],
            'productName': shown,
            'expiresAt': expires_at_iso,
            'daysRemaining': days_remaining,
            'serverTime': int(time.time()),
            'nonce': secrets.token_hex(8),
        }, 200)

    except Exception as e:
        return _respond({'success': False, 'error': str(e)}, 500)


# ============================================================================
# LICENSE CHECK - Kiểm tra trạng thái license
# ============================================================================

@license_bp.route('/check', methods=['POST'])
def check_license():
    """
    Kiểm tra trạng thái license.

    Request: cùng định dạng JSON/form thường như /activate.

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
            data = _parse_license_request_body()
        except ValueError as e:
            # App desktop có thể gọi check sớm khi chưa có key local.
            # Trả 200 + valid=false để không spam log 400.
            return _respond({'valid': False, 'error': str(e)}, 200)
        license_key = (data.get('licenseKey') or '').strip()
        machine_id = (data.get('machineId') or '').strip()
        machine_fingerprint = (data.get('machineFingerprint') or '').strip()

        if not license_key:
            return _respond({'valid': False, 'error': 'Thiếu license key'}, 200)

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
            return _respond({'valid': False, 'error': 'License không tồn tại'}, 401)

        # Verify machine binding
        if license['machine_id'] and license['machine_id'] != machine_id:
            cur.close()
            conn.close()
            return _respond({'valid': False, 'error': 'License đã bind với máy khác'}, 403)

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
        return _respond({
            'valid': valid,
            'productId': license['product_id'],
            'productName': shown,
            'productCategory': license.get('product_category'),
            'expiresAt': expires_at_iso,
            'daysRemaining': days_remaining,
            'serverTime': int(time.time()),
            'nonce': secrets.token_hex(8),
        }, 200)

    except Exception as e:
        return _respond({'valid': False, 'error': str(e)}, 500)


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

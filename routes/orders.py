from flask import Blueprint, request, jsonify, current_app, Response
import uuid
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from db import get_db
from auth import require_auth
from rate_limit import hit_rate, response_429
from crypto import generate_license_secret, hash_license_secret
import onedrive_graph
from routes.discounts import apply_discount_if_any, mark_discount_used_for_order

orders_bp = Blueprint('orders', __name__)

# Danh mục sản phẩm đồng bộ từ bảng sounds (id SP trùng id âm thanh) — không tạo license khi bán.
SOUND_STORE_CATEGORY = 'Âm thanh'


def is_sound_product(product) -> bool:
    if not product:
        return False
    cat = str(product.get('category') or '').strip().lower()
    return cat in ('âm thanh', 'am thanh', 'sound', 'sounds')


def generate_license_key(product_id=None):
    """
    Tạo license key theo product.
    - tc001 (Tiếng Cười Đông) -> TCDS-XXXXXXXX-XXXXXXXX
    - Các sản phẩm khác -> DS-XXXXXXXX-XXXXXXXX
    """
    uid = uuid.uuid4().hex[:16].upper()
    if product_id == 'tc001':
        return f'TCDS-{uid[:8]}-{uid[8:16]}'
    return f'DS-{uid[:8]}-{uid[8:16]}'


KEY_DURATION_OPTIONS = {
    '3_days': {'label': '3 ngày', 'days': 3},
    '1_month': {'label': '1 tháng', 'days': 30},
    '3_months': {'label': '3 tháng', 'days': 90},
    '1_year': {'label': '1 năm', 'days': 365},
}
DEFAULT_KEY_DURATION = '1_month'


def normalize_license_display_title(raw) -> Optional[str]:
    """Tên hiển thị trên app desktop (tiêu đề / validate payload n). Tối đa 120 ký tự."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return s[:120]


def _is_key_product(product) -> bool:
    category = str(product.get('category') or '').strip().lower()
    pid = str(product.get('id') or '').strip().lower()
    name = str(product.get('name') or '').strip().lower()

    has_duration_cfg = False
    try:
        raw_opts = product.get('duration_options')
        opts = json.loads(raw_opts) if isinstance(raw_opts, str) and raw_opts else (raw_opts or [])
        has_duration_cfg = has_duration_cfg or (isinstance(opts, list) and len(opts) > 0)
    except Exception:
        pass
    try:
        raw_prices = product.get('duration_prices')
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) and raw_prices else (raw_prices or {})
        has_duration_cfg = has_duration_cfg or (isinstance(prices, dict) and len(prices) > 0)
    except Exception:
        pass

    has_duration_cfg = has_duration_cfg or bool(product.get('require_duration'))

    return has_duration_cfg or category == 'license' or pid.startswith('key') or 'key' in name


def _resolve_key_duration(product, duration_code: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Trả về (duration_code, duration_label) theo setting sản phẩm key.
    - Sản phẩm thường: (None, None)
    - Key + require_duration=1: bắt buộc truyền duration hợp lệ
    - Key + require_duration=0: nếu không truyền thì lấy mặc định
    """
    if not _is_key_product(product):
        return None, None

    allowed = []
    try:
        raw = product.get('duration_options')
        allowed = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
    except Exception:
        allowed = []
    allowed = [str(x).strip() for x in allowed if str(x).strip() in KEY_DURATION_OPTIONS]
    if not allowed:
        allowed = list(KEY_DURATION_OPTIONS.keys())

    require_duration = bool(product.get('require_duration'))

    input_code = (duration_code or '').strip()
    if require_duration and not input_code:
        raise ValueError('Vui lòng chọn thời hạn trước khi mua')

    code = input_code or (allowed[0] if allowed else DEFAULT_KEY_DURATION)
    if code not in allowed:
        raise ValueError('duration không hợp lệ với sản phẩm này')

    return code, KEY_DURATION_OPTIONS[code]['label']


def _resolve_key_price(product, duration_code: Optional[str]) -> Optional[int]:
    if not duration_code:
        return None
    try:
        raw = product.get('duration_prices')
        prices = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except Exception:
        prices = {}
    if not isinstance(prices, dict):
        return None
    v = prices.get(duration_code)
    try:
        price = int(v)
    except Exception:
        return None
    return price if price > 0 else None


def _add_duration_to_expires(current_expires_at, duration_code: Optional[str]):
    if not duration_code:
        return current_expires_at
    meta = KEY_DURATION_OPTIONS.get(duration_code)
    if not meta:
        return current_expires_at
    base = current_expires_at if current_expires_at and current_expires_at > datetime.now(timezone.utc) else datetime.now(timezone.utc)
    return base + timedelta(days=meta['days'])


def _duration_to_expires_at(duration_code: Optional[str]):
    if not duration_code:
        return None
    meta = KEY_DURATION_OPTIONS.get(duration_code)
    if not meta:
        return None
    return datetime.now(timezone.utc) + timedelta(days=meta['days'])


def _create_license_record(
    order_id: str,
    product_id: str,
    user_id: str,
    license_key: str,
    duration_code: Optional[str] = None,
    display_title: Optional[str] = None,
) -> tuple:
    """
    Tạo license record trong bảng licenses.
    Trả về (license_key, license_secret) - secret chỉ trả về 1 lần duy nhất.
    """
    import secrets

    license_secret = secrets.token_urlsafe(32)[:32]
    license_secret_hash = hash_license_secret(license_secret)
    license_id = str(uuid.uuid4())

    conn = get_db()
    cur = conn.cursor()

    # Sản phẩm key có expiry theo thời hạn user chọn; sản phẩm khác giữ nguyên không expiry.
    expires_at = _duration_to_expires_at(duration_code)

    cur.execute("""
        INSERT INTO licenses (id, license_key, license_secret_hash, product_id, user_id, expires_at, status, display_title)
        VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
        ON CONFLICT (license_key) DO UPDATE
        SET license_secret_hash = EXCLUDED.license_secret_hash,
            user_id = EXCLUDED.user_id,
            status = 'active',
            activated_at = NOW(),
            expires_at = EXCLUDED.expires_at,
            display_title = COALESCE(EXCLUDED.display_title, licenses.display_title)
    """, (license_id, license_key, license_secret_hash, product_id, user_id, expires_at, display_title))

    conn.commit()
    cur.close()
    conn.close()

    # Trả về secret (chỉ 1 lần!)
    return license_key, license_secret


EXPIRE_MINUTES = 30

def _cancel_expired_pending_orders() -> int:
    """Hủy các đơn pending quá 30 phút. Trả về số đơn đã hủy."""
    conn = get_db()
    cur = conn.cursor()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EXPIRE_MINUTES)
    cur.execute(
        "UPDATE orders SET status = 'cancelled', updated_at = NOW() "
        "WHERE status = 'pending' AND created_at < %s",
        (cutoff,)
    )
    conn.commit()
    n = cur.rowcount
    if n > 0:
        print(f"[SCHEDULER] Auto-cancelled {n} expired pending order(s)")
    return n


cancel_expired_pending_orders = _cancel_expired_pending_orders  # exported for use by APScheduler in app.py


def _expire_check():
    """Gọi ở đầu mỗi endpoint đọc/xử lý đơn để dọn đơn quá hạn."""
    _cancel_expired_pending_orders()


def get_vietqr_url(
    amount: int,
    order_id: str,
    account_number: str,
    bank_id: str,
    account_name: str,
) -> str:
    """
    Tạo VietQR image URL.
    Dùng template từ config (mặc định: UH8kzQZ Quick Link).
    """
    name_raw = account_name.strip().upper()
    name_safe = ''.join(c for c in name_raw if c.isascii())
    template = current_app.config.get('VIETQR_TEMPLATE', 'UH8kzQZ')

    qr_img_url = (
        f"https://api.vietqr.io/image/{bank_id}-{account_number}-{template}.jpg"
        f"?accountName={urllib.parse.quote(name_safe, safe='')}"
        f"&amount={amount}"
        f"&addInfo={urllib.parse.quote(order_id, safe='')}"
    )
    return qr_img_url


def _payment_ui_for_order(order_id: str, amount: int):
    """Thông tin VietQR / ngân hàng cho một đơn (dùng khi tạo đơn mới hoặc tiếp tục thanh toán)."""
    bank_id = current_app.config.get('BANK_ID', '')
    account_number = current_app.config.get('BANK_ACCOUNT_NUMBER', '')
    account_name = current_app.config.get('BANK_ACCOUNT_NAME', '')

    qr_url = ''
    if account_number and bank_id:
        try:
            qr_url = get_vietqr_url(int(amount), order_id, account_number, bank_id, account_name)
        except Exception:
            qr_url = ''

    bank_name_map = {
        '970422': 'MB Bank',
        '970448': 'TPBank',
        '970436': 'Vietcombank',
        '970405': 'Citibank',
        '970431': 'VietinBank',
        '970418': 'VPBank',
        '970437': 'Sacombank',
        '970432': 'BIDV',
    }
    bank_display_name = bank_name_map.get(bank_id, f'Ngân hàng ({bank_id})')

    return {
        'transferContent': order_id,
        'totalPrice': int(amount),
        'bankName': bank_display_name if account_number else 'Thông tin chưa cấu hình',
        'accountNumber': account_number,
        'accountName': account_name,
        'qrUrl': qr_url,
    }


@orders_bp.route('/pending', methods=['POST'])
@require_auth
def create_pending_order():
    """
    Tạo đơn hàng ở trạng thái pending, trả về thông tin thanh toán VietQR.
    Frontend hiển thị QR cho user chuyển khoản, sau đó polling /orders/<id>.
    """
    # 30 req / 5 phút / user
    if not hit_rate("order_pending", 30, 300, request.user["id"]):
        return response_429(300)
    data = request.get_json() or {}
    product_id = data.get('productId')
    duration_code_input = data.get('duration')
    renew_key = (data.get('renewKey') or '').strip() or None
    display_title = normalize_license_display_title(data.get('displayTitle'))
    discount_code_input = data.get('discountCode')
    fixed_price_vnd = data.get('fixedPriceVnd')
    try:
        fixed_price_vnd = int(fixed_price_vnd) if fixed_price_vnd is not None else None
    except Exception:
        fixed_price_vnd = None
    if fixed_price_vnd is not None and fixed_price_vnd <= 0:
        fixed_price_vnd = None

    if not product_id:
        return jsonify({'error': 'productId là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    # Cho phép tạo order cả khi product chưa bật is_active (app desktop có thể dùng product seed nội bộ).
    cur.execute('SELECT * FROM products WHERE id = %s', (product_id,))
    product = cur.fetchone()

    if not product:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    # Sản phẩm tải miễn phí sau khi nạp tiền: không tạo order / không tạo key.
    # (Frontend sẽ dùng endpoint `/products/<id>/free-download`.)
    if bool(product.get('require_first_deposit')):
        return jsonify({
            'error': 'Sản phẩm này chỉ hỗ trợ tải miễn phí sau khi nạp tiền; không tạo đơn/không cần key.'
        }), 400

    if renew_key and is_sound_product(product):
        return jsonify({'error': 'Âm thanh không hỗ trợ gia hạn key'}), 400

    order_id = f"DH{str(uuid.uuid4().hex[:10]).upper()}"
    # Chưa tạo license_key ở đây — sẽ tạo khi thanh toán thành công (trừ đơn âm thanh)

    if is_sound_product(product):
        duration_code, duration_label = None, None
        selected_price = int(product['price'])
        base_price = int(product['price'])
        snapshot = {
            'name': product['name'],
            'emoji': product.get('emoji'),
            'category': product['category'],
            'duration_code': None,
            'duration_label': None,
            'allowed_duration_codes': [],
            'duration_prices': {},
            'selected_price': base_price,
            'renew_key': None,
            'require_duration': False,
            'is_sound': True,
        }
    else:
        try:
            duration_code, duration_label = _resolve_key_duration(product, duration_code_input)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        allowed_duration_codes = []
        try:
            raw_allowed = product.get('duration_options')
            allowed_duration_codes = json.loads(raw_allowed) if isinstance(raw_allowed, str) and raw_allowed else (raw_allowed or [])
        except Exception:
            allowed_duration_codes = []
        allowed_duration_codes = [str(x).strip() for x in allowed_duration_codes if str(x).strip() in KEY_DURATION_OPTIONS]
        if not allowed_duration_codes:
            allowed_duration_codes = list(KEY_DURATION_OPTIONS.keys())

        selected_price = _resolve_key_price(product, duration_code) or int(product['price'])

        if renew_key:
            if not bool(product.get('require_duration')):
                return jsonify({'error': 'Sản phẩm này không hỗ trợ gia hạn'}), 400
            cur.execute(
                "SELECT id, product_id, user_id, status FROM licenses WHERE license_key = %s",
                (renew_key,),
            )
            lk = cur.fetchone()
            if not lk:
                return jsonify({'error': 'Key cần gia hạn không tồn tại'}), 404
            if lk.get('user_id') != request.user['id']:
                return jsonify({'error': 'Bạn không sở hữu key này'}), 403
            if lk.get('product_id') != product_id:
                return jsonify({'error': 'Key không thuộc sản phẩm đã chọn'}), 400
            if (lk.get('status') or 'active') != 'active':
                return jsonify({'error': 'Key đã bị vô hiệu hóa, không thể gia hạn'}), 400

        snapshot = {
            'name': product['name'],
            'emoji': product.get('emoji'),
            'category': product['category'],
            'duration_code': duration_code,
            'duration_label': duration_label,
            'allowed_duration_codes': allowed_duration_codes,
            'duration_prices': json.loads(product['duration_prices']) if isinstance(product.get('duration_prices'), str) and product.get('duration_prices') else {},
            'selected_price': selected_price,
            'renew_key': renew_key,
            'require_duration': bool(product.get('require_duration')),
        }
        if display_title:
            snapshot['display_title'] = display_title

    base_total = selected_price
    if fixed_price_vnd is not None:
        # Ép giá cố định để app/desktop thu đúng số tiền.
        selected_price = fixed_price_vnd
        snapshot['selected_price'] = fixed_price_vnd
        base_total = fixed_price_vnd
        final_total = fixed_price_vnd
        applied_code = None
        discount_amount = 0
    else:
        final_total, applied_code, discount_amount, discount_error = apply_discount_if_any(
            discount_code_input, base_total, product_id, user_id=request.user['id']
        )
        if discount_error:
            return jsonify({'error': discount_error}), 400

    cur.execute("""
        INSERT INTO orders (id, user_id, product_id, product_snapshot, total_price, discount_code, discount_amount, status, payment_method, license_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 'vietqr', NULL)
    """, (order_id, request.user['id'], product_id, json.dumps(snapshot),
          final_total, applied_code, discount_amount))
    conn.commit()

    pay = _payment_ui_for_order(order_id, final_total)

    return jsonify({
        'id': order_id,
        'status': 'pending',
        'transferContent': pay['transferContent'],
        'totalPrice': pay['totalPrice'],
        'bankName': pay['bankName'],
        'accountNumber': pay['accountNumber'],
        'accountName': pay['accountName'],
        'qrUrl': pay['qrUrl'],
    }), 201


@orders_bp.route('', methods=['GET'])
@require_auth
def get_orders():
    _expire_check()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.*, p.name as product_name, p.emoji as product_emoji, p.category as product_category,
               p.require_duration as product_require_duration,
               p.app_download_url as product_app_download_url,
               p.app_installer_path as product_app_installer_path,
               l.expires_at as license_expires_at, l.status as license_status,
               l.machine_id as license_machine_id,
               l.free_hwid_reset_after as license_free_hwid_reset_after
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.id
        LEFT JOIN licenses l ON o.license_key = l.license_key AND l.user_id = o.user_id
        WHERE o.user_id = %s
        ORDER BY o.created_at DESC
    """, (request.user['id'],))
    rows = cur.fetchall()

    orders = []
    for r in rows:
        snapshot = None
        try:
            snapshot = json.loads(r['product_snapshot']) if r['product_snapshot'] else None
        except Exception:
            pass
        if snapshot is not None and 'require_duration' in snapshot:
            require_duration_flag = bool(snapshot.get('require_duration'))
        else:
            prd = r.get('product_require_duration')
            # Đơn cũ không có snapshot.require_duration và cột DB null → giả định bắt buộc chọn thời hạn (hành vi gia hạn như trước)
            require_duration_flag = bool(prd) if prd is not None else True
        mid = str(r.get('license_machine_id') or '').strip()
        fraw = r.get('license_free_hwid_reset_after')
        free_hwid_after = None
        if fraw is not None:
            if hasattr(fraw, 'isoformat'):
                free_hwid_after = fraw.isoformat()
            else:
                free_hwid_after = str(fraw)
        inst_path = (r.get('product_app_installer_path') or '').strip()
        raw_app_url = r.get('product_app_download_url')
        legacy_dl = raw_app_url.strip() if isinstance(raw_app_url, str) and raw_app_url.strip() else None
        app_dl = None if inst_path else legacy_dl
        cat_ord = snapshot['category'] if snapshot else r['product_category']
        orders.append({
            'id': r['id'],
            'productId': r.get('product_id'),
            'product': snapshot['name'] if snapshot else r['product_name'],
            'emoji': snapshot['emoji'] if snapshot else r['product_emoji'],
            'category': cat_ord,
            'is_sound_order': is_sound_product({'category': cat_ord}),
            'duration': snapshot.get('duration_code') if snapshot else None,
            'durationLabel': snapshot.get('duration_label') if snapshot else None,
            'expiresAt': r['license_expires_at'].isoformat() if r.get('license_expires_at') and hasattr(r['license_expires_at'], 'isoformat') else (str(r['license_expires_at']) if r.get('license_expires_at') else None),
            'keyStatus': (r.get('license_status') if r.get('license_status') else ('deleted' if r.get('license_key') else None)),
            'date': r['created_at'],
            'price': r['total_price'],
            'status': r['status'],
            'key': r['license_key'] if r['status'] in ('completed', 'paid') else None,
            'requireDuration': require_duration_flag,
            'machineId': mid or None,
            'keyActivated': bool(mid),
            'freeHwidResetAfter': free_hwid_after,
            'appDownloadUrl': app_dl,
            'appInstallerAvailable': bool(inst_path),
        })

    return jsonify({'orders': orders})


@orders_bp.route('/<order_id>', methods=['GET'])
@require_auth
def get_order(order_id):
    _expire_check()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.*, p.name as product_name, p.category as product_category, p.image as product_image,
               p.app_download_url as product_app_download_url,
               p.app_installer_path as product_app_installer_path
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.id
        WHERE o.id = %s AND o.user_id = %s
    """, (order_id, request.user['id']))
    r = cur.fetchone()

    if not r:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404

    snapshot = None
    try:
        snapshot = json.loads(r['product_snapshot']) if r['product_snapshot'] else None
    except Exception:
        pass

    inst_path = (r.get('product_app_installer_path') or '').strip()
    raw_app = r.get('product_app_download_url')
    legacy_dl = raw_app.strip() if isinstance(raw_app, str) and raw_app.strip() else None
    app_dl = None if inst_path else legacy_dl

    cat_for_flag = (snapshot.get('category') if snapshot else None) or r.get('product_category')
    payload = {
        'id': r['id'],
        'product_id': r['product_id'],
        'product': snapshot['name'] if snapshot else r['product_name'],
        'category': snapshot['category'] if snapshot else r['product_category'],
        'product_snapshot': snapshot,
        'status': r['status'],
        'license_key': r['license_key'] if r['status'] in ('completed', 'paid') else None,
        'total_price': r['total_price'],
        'payment_method': r['payment_method'],
        'created_at': r['created_at'],
        # Ảnh từ join (sản phẩm còn trong DB) — snapshot cũ có thể không có image
        'product_image': r.get('product_image'),
        'duration': snapshot.get('duration_code') if snapshot else None,
        'durationLabel': snapshot.get('duration_label') if snapshot else None,
        'appDownloadUrl': app_dl,
        'appInstallerAvailable': bool(inst_path),
        'is_sound_order': is_sound_product({'category': cat_for_flag}),
    }

    # Đơn pending: trả luôn QR / CK để frontend không cần gọi API sản phẩm (có thể 404 nếu SP đã ẩn)
    if r['status'] == 'pending':
        pay = _payment_ui_for_order(r['id'], r['total_price'])
        payload['transferContent'] = pay['transferContent']
        payload['totalPrice'] = pay['totalPrice']
        payload['bankName'] = pay['bankName']
        payload['accountNumber'] = pay['accountNumber']
        payload['accountName'] = pay['accountName']
        payload['qrUrl'] = pay['qrUrl']

    return jsonify(payload)


@orders_bp.route('/<order_id>/apply-discount', methods=['POST'])
@require_auth
def apply_discount(order_id):
    """
    Áp dụng / thay đổi mã giảm giá cho đơn pending hiện tại.
    Cập nhật lại total_price, discount_code, discount_amount và thông tin VietQR.
    """
    if not hit_rate("order_apply_discount", 40, 60, request.user["id"]):
        return response_429(60)
    _expire_check()
    data = request.get_json() or {}
    raw_code = data.get('code') or data.get('discountCode') or ''

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, status, total_price, COALESCE(discount_amount, 0) AS discount_amount, product_id
        FROM orders
        WHERE id = %s AND user_id = %s
        """,
        (order_id, request.user['id']),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404
    if str(row.get('status') or '') != 'pending':
        cur.close()
        conn.close()
        return jsonify({'error': 'Chỉ áp dụng mã cho đơn đang chờ thanh toán'}), 400

    current_total = int(row.get('total_price') or 0)
    current_discount = int(row.get('discount_amount') or 0)
    base_total = current_total + max(current_discount, 0)

    final_total, applied_code, discount_amount, discount_error = apply_discount_if_any(
        raw_code,
        base_total,
        row.get('product_id'),
        user_id=request.user['id'],
        exclude_order_id=order_id,
    )
    if discount_error:
        cur.close()
        conn.close()
        return jsonify({'error': discount_error}), 400

    cur.execute(
        """
        UPDATE orders
        SET total_price = %s,
            discount_code = %s,
            discount_amount = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (final_total, applied_code, discount_amount, order_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    pay = _payment_ui_for_order(order_id, final_total)
    return jsonify(
        {
            'id': order_id,
            'status': 'pending',
            'transferContent': pay['transferContent'],
            'totalPrice': pay['totalPrice'],
            'bankName': pay['bankName'],
            'accountNumber': pay['accountNumber'],
            'accountName': pay['accountName'],
            'qrUrl': pay['qrUrl'],
            'discountCode': applied_code,
            'discountAmount': discount_amount,
        }
    )


@orders_bp.route('/<order_id>/app-installer', methods=['GET'])
@require_auth
def get_order_app_installer(order_id):
    """Trả link tải tạm từ Graph (Bearer bắt buộc — không dùng được <a href> trần)."""
    _expire_check()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.status, o.user_id, p.app_installer_path, p.app_download_url
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.id
        WHERE o.id = %s
        """,
        (order_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404
    if row.get('user_id') != request.user['id']:
        return jsonify({'error': 'Forbidden'}), 403
    st = row.get('status') or ''
    if st not in ('completed', 'paid'):
        return jsonify({'error': 'Đơn chưa hoàn thành'}), 400
    path = (row.get('app_installer_path') or '').strip()
    if path:
        if not onedrive_graph.onedrive_configured():
            return jsonify({'error': 'OneDrive chưa cấu hình'}), 503
        url = onedrive_graph.graph_get_download_url(path)
        if not url:
            return jsonify({'error': 'Không lấy được link tải từ OneDrive'}), 502
        return jsonify({'downloadUrl': url})
    legacy = row.get('app_download_url')
    if isinstance(legacy, str) and legacy.strip():
        return jsonify({'downloadUrl': legacy.strip()})
    return jsonify({'error': 'Sản phẩm chưa có file tải'}), 404


@orders_bp.route('/<order_id>/sound-download', methods=['GET'])
@require_auth
def download_purchased_sound(order_id):
    """Tải file gốc âm thanh — chỉ khi đơn đã hoàn thành và là sản phẩm Âm thanh."""
    from upload import stream_sound

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.id, o.user_id, o.status, o.product_id, p.category AS product_category
        FROM orders o
        JOIN products p ON o.product_id = p.id
        WHERE o.id = %s AND o.user_id = %s
        """,
        (order_id, request.user['id']),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404
    st = str(row.get('status') or '')
    if st not in ('completed', 'paid'):
        cur.close()
        conn.close()
        return jsonify({'error': 'Đơn chưa hoàn thành'}), 400
    if not is_sound_product({'category': row.get('product_category')}):
        cur.close()
        conn.close()
        return jsonify({'error': 'Đơn không phải âm thanh mua riêng'}), 400

    sound_id = row['product_id']
    cur.execute(
        'SELECT object_name, storage, original_filename, name FROM sounds WHERE id = %s AND is_active = 1',
        (sound_id,),
    )
    snd = cur.fetchone()
    cur.close()
    conn.close()
    if not snd:
        return jsonify({'error': 'Âm thanh không còn trên hệ thống'}), 404

    try:
        resp, content_type = stream_sound(
            snd['object_name'], storage=snd.get('storage') or 'minio'
        )
        body = resp.read() if hasattr(resp, 'read') else resp
        if hasattr(resp, 'close'):
            try:
                resp.close()
            except Exception:
                pass
    except ValueError:
        return jsonify({'error': 'File không tìm thấy'}), 404

    raw_name = (snd.get('original_filename') or '').strip() or f"{snd.get('name') or 'sound'}.mp3"
    safe_name = raw_name.replace('"', '').replace('\r', '').replace('\n', '')
    out = Response(body, mimetype=content_type or 'audio/mpeg')
    out.headers['Content-Disposition'] = f'attachment; filename="{safe_name}"'
    return out


@orders_bp.route('', methods=['POST'])
@require_auth
def create_order():
    data = request.get_json() or {}
    product_id = data.get('productId')
    payment_method = data.get('paymentMethod', 'banking')
    duration_code_input = data.get('duration')
    display_title = normalize_license_display_title(data.get('displayTitle'))
    discount_code_input = data.get('discountCode')

    if not product_id:
        return jsonify({'error': 'productId là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s AND is_active = 1', (product_id,))
    product = cur.fetchone()

    if not product:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    # Sản phẩm tải miễn phí sau khi nạp tiền: không tạo order / không tạo key.
    if bool(product.get('require_first_deposit')):
        return jsonify({
            'error': 'Sản phẩm này chỉ hỗ trợ tải miễn phí sau khi nạp tiền; không tạo đơn/không cần key.'
        }), 400

    order_id = f"DH{str(uuid.uuid4().hex[:10]).upper()}"

    if is_sound_product(product):
        selected_price = int(product['price'])
        snapshot = {
            'name': product['name'],
            'emoji': product['emoji'],
            'category': product['category'],
            'duration_code': None,
            'duration_label': None,
            'allowed_duration_codes': [],
            'duration_prices': {},
            'selected_price': selected_price,
            'require_duration': False,
            'is_sound': True,
        }
        base_total = selected_price
        final_total, applied_code, discount_amount, discount_error = apply_discount_if_any(
            discount_code_input, base_total, product_id, user_id=request.user['id']
        )
        if discount_error:
            return jsonify({'error': discount_error}), 400
        cur.execute("""
            INSERT INTO orders (id, user_id, product_id, product_snapshot, total_price, discount_code, discount_amount, status, payment_method, license_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed', %s, NULL)
        """, (order_id, request.user['id'], product_id, json.dumps(snapshot),
              final_total, applied_code, discount_amount, payment_method))
        conn.commit()
        mark_discount_used_for_order(order_id)
        cur.close()
        conn.close()
        return jsonify({
            'order': {
                'id': order_id,
                'product': snapshot['name'],
                'emoji': snapshot['emoji'],
                'category': snapshot['category'],
                'date': None,
                'price': final_total,
                'status': 'completed',
                'key': None,
            },
            'licenseKey': None,
            'isSoundOrder': True,
        }), 201

    try:
        duration_code, duration_label = _resolve_key_duration(product, duration_code_input)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    license_key = generate_license_key(product_id)

    allowed_duration_codes = []
    try:
        raw_allowed = product.get('duration_options')
        allowed_duration_codes = json.loads(raw_allowed) if isinstance(raw_allowed, str) and raw_allowed else (raw_allowed or [])
    except Exception:
        allowed_duration_codes = []
    allowed_duration_codes = [str(x).strip() for x in allowed_duration_codes if str(x).strip() in KEY_DURATION_OPTIONS]
    if not allowed_duration_codes:
        allowed_duration_codes = list(KEY_DURATION_OPTIONS.keys())

    selected_price = _resolve_key_price(product, duration_code) or int(product['price'])

    snapshot = {
        'name': product['name'],
        'emoji': product['emoji'],
        'category': product['category'],
        'duration_code': duration_code,
        'duration_label': duration_label,
        'allowed_duration_codes': allowed_duration_codes,
        'duration_prices': json.loads(product['duration_prices']) if isinstance(product.get('duration_prices'), str) and product.get('duration_prices') else {},
        'selected_price': selected_price,
        'require_duration': bool(product.get('require_duration')),
    }
    if display_title:
        snapshot['display_title'] = display_title

    base_total = selected_price
    final_total, applied_code, discount_amount, discount_error = apply_discount_if_any(
        discount_code_input, base_total, product_id, user_id=request.user['id']
    )
    if discount_error:
        return jsonify({'error': discount_error}), 400

    cur.execute("""
        INSERT INTO orders (id, user_id, product_id, product_snapshot, total_price, discount_code, discount_amount, status, payment_method, license_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed', %s, %s)
    """, (order_id, request.user['id'], product_id, json.dumps(snapshot),
          final_total, applied_code, discount_amount, payment_method, license_key))

    # Tạo license record với secret
    _, license_secret = _create_license_record(
        order_id, product_id, request.user['id'], license_key, duration_code, display_title
    )

    conn.commit()
    mark_discount_used_for_order(order_id)

    return jsonify({
        'order': {
            'id': order_id,
            'product': snapshot['name'],
            'emoji': snapshot['emoji'],
            'category': snapshot['category'],
            'date': None,
            'price': final_total,
            'status': 'completed',
            'key': license_key,
        },
        'licenseKey': license_key,
        'licenseSecret': license_secret,  # Chỉ trả về 1 lần duy nhất!
    }), 201

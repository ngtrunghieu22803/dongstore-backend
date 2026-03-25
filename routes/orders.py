from flask import Blueprint, request, jsonify, current_app
import uuid
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from db import get_db
from auth import require_auth

orders_bp = Blueprint('orders', __name__)


def generate_license_key(prefix='DS'):
    uid = uuid.uuid4().hex[:16].upper()
    return f'{prefix}-{uid[:8]}-{uid[8:16]}'


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
    Dùng template cố định: cbcvvO1.jpg
    Chỉ amount và addInfo (nội dung CK) thay đổi theo đơn.
    """
    name_raw = account_name.strip().upper()
    name_safe = ''.join(c for c in name_raw if c.isascii())

    qr_img_url = (
        f"https://api.vietqr.io/image/{bank_id}-{account_number}-compact2.jpg"
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
    data = request.get_json() or {}
    product_id = data.get('productId')

    if not product_id:
        return jsonify({'error': 'productId là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s AND is_active = 1', (product_id,))
    product = cur.fetchone()

    if not product:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    order_id = f"DH{str(uuid.uuid4().hex[:10]).upper()}"
    # Chưa tạo license_key ở đây — sẽ tạo khi thanh toán thành công

    snapshot = {
        'name': product['name'],
        'emoji': product.get('emoji'),
        'category': product['category'],
    }

    cur.execute("""
        INSERT INTO orders (id, user_id, product_id, product_snapshot, total_price, status, payment_method, license_key)
        VALUES (%s, %s, %s, %s, %s, 'pending', 'vietqr', NULL)
    """, (order_id, request.user['id'], product_id, json.dumps(snapshot),
          product['price']))
    conn.commit()

    pay = _payment_ui_for_order(order_id, product['price'])

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
        SELECT o.*, p.name as product_name, p.emoji as product_emoji, p.category as product_category
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.id
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
        orders.append({
            'id': r['id'],
            'product': snapshot['name'] if snapshot else r['product_name'],
            'emoji': snapshot['emoji'] if snapshot else r['product_emoji'],
            'category': snapshot['category'] if snapshot else r['product_category'],
            'date': r['created_at'],
            'price': r['total_price'],
            'status': r['status'],
            'key': r['license_key'] if r['status'] in ('completed', 'paid') else None,
        })

    return jsonify({'orders': orders})


@orders_bp.route('/<order_id>', methods=['GET'])
@require_auth
def get_order(order_id):
    _expire_check()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.*, p.name as product_name, p.category as product_category, p.image as product_image
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


@orders_bp.route('', methods=['POST'])
@require_auth
def create_order():
    data = request.get_json() or {}
    product_id = data.get('productId')
    payment_method = data.get('paymentMethod', 'banking')

    if not product_id:
        return jsonify({'error': 'productId là bắt buộc'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s AND is_active = 1', (product_id,))
    product = cur.fetchone()

    if not product:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    order_id = f"DH{str(uuid.uuid4().hex[:10]).upper()}"
    license_key = generate_license_key(product_id[:2].upper())

    snapshot = {
        'name': product['name'],
        'emoji': product['emoji'],
        'category': product['category'],
    }

    cur.execute("""
        INSERT INTO orders (id, user_id, product_id, product_snapshot, total_price, status, payment_method, license_key)
        VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s)
    """, (order_id, request.user['id'], product_id, json.dumps(snapshot),
          product['price'], payment_method, license_key))
    conn.commit()

    return jsonify({
        'order': {
            'id': order_id,
            'product': snapshot['name'],
            'emoji': snapshot['emoji'],
            'category': snapshot['category'],
            'date': None,
            'price': product['price'],
            'status': 'completed',
            'key': license_key,
        },
        'licenseKey': license_key,
    }), 201

from flask import Blueprint, request, jsonify
import uuid
import json
from datetime import datetime, timedelta, timezone
from db import get_db
from auth import require_auth
from routes.orders import is_sound_product

PAYMENT_EXPIRE_MINUTES = 30

def _cancel_expired_pending_orders() -> int:
    conn = get_db()
    cur = conn.cursor()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PAYMENT_EXPIRE_MINUTES)
    cur.execute(
        "UPDATE orders SET status = 'cancelled', updated_at = NOW() "
        "WHERE status = 'pending' AND created_at < %s",
        (cutoff,)
    )
    conn.commit()
    return cur.rowcount

payments_bp = Blueprint('payments', __name__)


def generate_license_key(prefix='DS'):
    uid = uuid.uuid4().hex[:16].upper()
    return f'{prefix}-{uid[:8]}-{uid[8:16]}'


@payments_bp.route('/create-order', methods=['POST'])
@require_auth
def create_payment_order():
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

    payment_id = f"PAY{uuid.uuid4().hex[:10].upper()}"
    order_id = f"DH{uuid.uuid4().hex[:10].upper()}"
    # Chưa tạo license_key ở đây — sẽ tạo khi thanh toán thành công

    snapshot = {
        'name': product['name'],
        'emoji': product['emoji'],
        'category': product['category'],
    }

    cur.execute("""
        INSERT INTO orders (id, user_id, product_id, product_snapshot, total_price, status, payment_method, payment_id, license_key)
        VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, NULL)
    """, (order_id, request.user['id'], product_id, json.dumps(snapshot),
          product['price'], payment_method, payment_id))
    conn.commit()

    payment_info = {
        'banking': {
            'type': 'banking',
            'instructions': [
                f'1. Chuyển khoản đến: VietinBank - STK: 123456789 - NGUYEN VAN A',
                f'2. Nội dung: {order_id}',
                f'3. Số tiền: {product["price"]:,.0f} VND',
            ],
            'note': 'Sau khi chuyển khoản, nhấn "Xác nhận thanh toán" để nhận sản phẩm ngay',
        },
        'momo': {
            'type': 'momo',
            'amount': product['price'],
            'orderId': payment_id,
            'note': 'Thanh toán qua MoMo (cần tích hợp thực tế)',
        },
        'vnpay': {
            'type': 'vnpay',
            'amount': product['price'],
            'orderId': payment_id,
            'note': 'Thanh toán qua VNPay (cần tích hợp thực tế)',
        },
    }

    pm = payment_info.get(payment_method, payment_info['banking'])

    return jsonify({
        'orderId': order_id,
        'paymentId': payment_id,
        'amount': product['price'],
        'product': {
            'name': product['name'],
            'emoji': product['emoji'],
        },
        'payment': pm,
    }), 201


@payments_bp.route('/callback', methods=['POST'])
def payment_callback():
    data = request.get_json() or {}
    payment_id = data.get('paymentId')
    order_id = data.get('orderId')
    status = data.get('status')

    if not payment_id or not order_id:
        return jsonify({'error': 'Thiếu thông tin thanh toán'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM orders WHERE id = %s AND payment_id = %s', (order_id, payment_id))
    order = cur.fetchone()

    if not order:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404

    new_status = 'completed' if status in ('success', 'completed') else 'cancelled'

    if new_status == 'completed':
        cur.execute('SELECT * FROM products WHERE id = %s', (order['product_id'],))
        prod = cur.fetchone()
        if prod and is_sound_product(prod):
            cur.execute(
                'UPDATE orders SET status = %s, license_key = NULL, updated_at = NOW() WHERE id = %s',
                (new_status, order_id),
            )
        else:
            license_key = generate_license_key(order['product_id'][:2].upper())
            cur.execute(
                'UPDATE orders SET status = %s, license_key = %s, updated_at = NOW() WHERE id = %s',
                (new_status, license_key, order_id),
            )
    else:
        cur.execute('UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s', (new_status, order_id))
    conn.commit()

    return jsonify({'success': True})


@payments_bp.route('/confirm', methods=['POST'])
@require_auth
def confirm_payment():
    data = request.get_json() or {}
    order_id = data.get('orderId')

    if not order_id:
        return jsonify({'error': 'orderId là bắt buộc'}), 400

    _cancel_expired_pending_orders()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM orders WHERE id = %s AND user_id = %s', (order_id, request.user['id']))
    order = cur.fetchone()

    if not order:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404

    if order['status'] != 'pending':
        return jsonify({'error': 'Đơn hàng không ở trạng thái chờ thanh toán'}), 400

    snapshot = None
    try:
        snapshot = json.loads(order['product_snapshot']) if order['product_snapshot'] else {}
    except Exception:
        snapshot = {}

    cur.execute('SELECT * FROM products WHERE id = %s', (order['product_id'],))
    product = cur.fetchone()

    if product and is_sound_product(product):
        cur.execute(
            'UPDATE orders SET status = %s, license_key = NULL, updated_at = NOW() WHERE id = %s',
            ('completed', order_id),
        )
        conn.commit()
        return jsonify({
            'success': True,
            'licenseKey': None,
            'isSoundOrder': True,
            'order': {
                'id': order_id,
                'product': snapshot.get('name', ''),
                'date': order['created_at'],
                'price': order['total_price'],
                'status': 'completed',
                'key': None,
            },
        })

    license_key = generate_license_key(order['product_id'][:2].upper())
    cur.execute(
        'UPDATE orders SET status = %s, license_key = %s, updated_at = NOW() WHERE id = %s',
        ('completed', license_key, order_id)
    )
    conn.commit()

    return jsonify({
        'success': True,
        'licenseKey': license_key,
        'order': {
            'id': order_id,
            'product': snapshot.get('name', ''),
            'date': order['created_at'],
            'price': order['total_price'],
            'status': 'completed',
            'key': license_key,
        },
    })


import json

from flask import Blueprint, request, jsonify
import uuid
import json
from db import get_db
from auth import require_auth

orders_bp = Blueprint('orders', __name__)


def generate_license_key(prefix='DS'):
    uid = uuid.uuid4().hex[:16].upper()
    return f'{prefix}-{uid[:8]}-{uid[8:16]}'


@orders_bp.route('', methods=['GET'])
@require_auth
def get_orders():
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
            'key': r['license_key'],
        })

    return jsonify({'orders': orders})


@orders_bp.route('/<order_id>', methods=['GET'])
@require_auth
def get_order(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.*, p.name as product_name
        FROM orders o
        LEFT JOIN products p ON o.product_id = p.id
        WHERE o.id = %s AND o.user_id = %s
    """, (order_id, request.user['id']))
    r = cur.fetchone()

    if not r:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404

    return jsonify(dict(r))


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


import json

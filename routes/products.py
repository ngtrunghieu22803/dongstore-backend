from flask import Blueprint, request, jsonify
import json
from db import get_db

products_bp = Blueprint('products', __name__)


@products_bp.route('', methods=['GET'])
def get_products():
    category = request.args.get('category', '')
    conn = get_db()
    cur = conn.cursor()

    if category and category != 'Tất cả':
        cur.execute(
            'SELECT * FROM products WHERE is_active = 1 AND category = %s ORDER BY created_at DESC',
            (category,)
        )
    else:
        cur.execute('SELECT * FROM products WHERE is_active = 1 ORDER BY created_at DESC')

    rows = cur.fetchall()
    products = []
    for r in rows:
        p = dict(r)
        try:
            p['features'] = json.loads(p['features']) if p['features'] else []
        except Exception:
            p['features'] = []
        products.append(p)

    return jsonify({'products': products})


@products_bp.route('/<product_id>', methods=['GET'])
def get_product(product_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s AND is_active = 1', (product_id,))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    p = dict(row)
    try:
        p['features'] = json.loads(p['features']) if p['features'] else []
    except Exception:
        p['features'] = []
    return jsonify(p)


@products_bp.route('/categories', methods=['GET'])
def get_categories():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT category FROM products WHERE is_active = 1 ORDER BY category')
    rows = cur.fetchall()
    categories = ['Tất cả'] + [r['category'] for r in rows]
    return jsonify({'categories': categories})

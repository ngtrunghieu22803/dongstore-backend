"""
Admin routes: quản lý sản phẩm (CRUD). Yêu cầu role='admin'.
"""
import json
import uuid
from flask import Blueprint, request, jsonify
from db import get_db
from auth import require_auth
import upload

admin_bp = Blueprint('admin', __name__)


def _require_admin(f):
    """Decorator yêu cầu role admin."""
    from functools import wraps

    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        # request.user là dict (auth.require_auth), không phải object — getattr(..., 'role') luôn None → 403 sai
        if request.user.get('role') != 'admin':
            return jsonify({'error': 'Admin only'}), 403
        return f(*args, **kwargs)

    return decorated


@admin_bp.route('/products', methods=['GET'])
@_require_admin
def list_products():
    """Danh sách tất cả sản phẩm (bao gồm inactive)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products ORDER BY created_at DESC')
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


@admin_bp.route('/products', methods=['POST'])
@_require_admin
def create_product():
    """Tạo sản phẩm mới."""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    category = data.get('category', '').strip()
    price = int(data.get('price', 0))
    original_price = int(data.get('original_price', 0)) or 0
    badge = data.get('badge', '').strip() or None
    emoji = data.get('emoji', '').strip() or None
    description = data.get('description', '').strip() or None
    features = data.get('features', [])
    image = data.get('image', '').strip() or None
    video_url = data.get('video_url', '').strip() or None
    stock = int(data.get('stock', -1))
    is_active = 1 if data.get('is_active', True) else 0

    if not name or not category or price <= 0:
        return jsonify({'error': 'name, category, price là bắt buộc'}), 400

    product_id = data.get('id', '').strip() or f"prod{uuid.uuid4().hex[:8]}"

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO products (id, name, category, price, original_price, badge, emoji, description, features, image, video_url, stock, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                product_id,
                name,
                category,
                price,
                original_price,
                badge,
                emoji,
                description,
                json.dumps(features, ensure_ascii=False),
                image,
                video_url,
                stock,
                is_active,
            ),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'error': f'Tạo sản phẩm thất bại: {str(e)}'}), 400

    return jsonify({'success': True, 'product_id': product_id}), 201


@admin_bp.route('/products/<product_id>', methods=['GET'])
@_require_admin
def get_product(product_id):
    """Chi tiết sản phẩm."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s', (product_id,))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    p = dict(row)
    try:
        p['features'] = json.loads(p['features']) if p['features'] else []
    except Exception:
        p['features'] = []
    return jsonify(p)


@admin_bp.route('/products/<product_id>', methods=['PUT'])
@_require_admin
def update_product(product_id):
    """Cập nhật sản phẩm."""
    data = request.get_json() or {}

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM products WHERE id = %s', (product_id,))
    if not cur.fetchone():
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    updates = []
    params = []

    for field in ['name', 'category', 'price', 'original_price', 'badge', 'emoji', 'description', 'image', 'video_url', 'stock', 'is_active']:
        if field in data:
            val = data[field]
            if field in ('price', 'original_price', 'stock'):
                val = int(val) if val else 0
            elif field == 'is_active':
                val = 1 if val else 0
            elif field == 'features':
                val = json.dumps(val, ensure_ascii=False) if isinstance(val, list) else val
            elif isinstance(val, str):
                val = val.strip() or None
            updates.append(f'{field} = %s')
            params.append(val)

    if not updates:
        return jsonify({'error': 'Không có thông tin cập nhật'}), 400

    params.append(product_id)
    sql = f"UPDATE products SET {', '.join(updates)} WHERE id = %s"
    cur.execute(sql, params)
    conn.commit()

    return jsonify({'success': True})


@admin_bp.route('/products/<product_id>', methods=['DELETE'])
@_require_admin
def delete_product(product_id):
    """Xóa sản phẩm (chuyển is_active = 0)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE products SET is_active = 0 WHERE id = %s', (product_id,))
    conn.commit()

    if cur.rowcount == 0:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    return jsonify({'success': True})


@admin_bp.route('/products/<product_id>/toggle', methods=['POST'])
@_require_admin
def toggle_product(product_id):
    """Bật/tắt sản phẩm."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT is_active FROM products WHERE id = %s', (product_id,))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    new_status = 0 if row['is_active'] else 1
    cur.execute('UPDATE products SET is_active = %s WHERE id = %s', (new_status, product_id))
    conn.commit()

    return jsonify({'success': True, 'is_active': new_status == 1})


# ─── Orders / Users (admin list) ─────────────────────────────────────────────

@admin_bp.route('/orders', methods=['GET'])
@_require_admin
def list_orders_admin():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.*, u.email AS user_email, u.name AS user_name,
               p.name AS product_name, p.emoji AS product_emoji
        FROM orders o
        LEFT JOIN users u ON o.user_id = u.id
        LEFT JOIN products p ON o.product_id = p.id
        ORDER BY o.created_at DESC
        """
    )
    rows = cur.fetchall()
    orders = []
    for r in rows:
        snap = None
        try:
            snap = json.loads(r['product_snapshot']) if r['product_snapshot'] else None
        except Exception:
            pass
        pname = (snap or {}).get('name') or r['product_name']
        orders.append({
            'id': r['id'],
            'user_id': r['user_id'],
            'userId': r['user_id'],
            'userEmail': r['user_email'],
            'userName': r['user_name'],
            'productId': r['product_id'],
            'product': pname,
            'product_name': pname,
            'productName': pname,
            'emoji': (snap or {}).get('emoji') or r['product_emoji'],
            'total_price': r['total_price'],
            'totalPrice': r['total_price'],
            'price': r['total_price'],
            'status': r['status'],
            'licenseKey': r['license_key'],
            'key': r['license_key'],
            'createdAt': r['created_at'].isoformat() if r.get('created_at') else None,
            'date': r['created_at'],
        })
    return jsonify({'orders': orders})


@admin_bp.route('/users', methods=['GET'])
@_require_admin
def list_users_admin():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, email, name, role, created_at FROM users ORDER BY created_at DESC'
    )
    rows = cur.fetchall()
    users = [dict(r) for r in rows]
    for u in users:
        if u.get('created_at'):
            u['createdAt'] = u['created_at'].isoformat() if hasattr(u['created_at'], 'isoformat') else str(u['created_at'])
    return jsonify({'users': users})


@admin_bp.route('/stats/dashboard', methods=['GET'])
@_require_admin
def stats_dashboard():
    conn = get_db()
    cur = conn.cursor()

    def _count(sql):
        cur.execute(sql)
        row = cur.fetchone()
        return row['c'] if isinstance(row, dict) else row[0]

    return jsonify({
        'products': _count('SELECT COUNT(*) AS c FROM products'),
        'hiddenProducts': _count("SELECT COUNT(*) AS c FROM products WHERE is_active = 0"),
        'visibleProducts': _count("SELECT COUNT(*) AS c FROM products WHERE is_active = 1"),
        'orders': _count('SELECT COUNT(*) AS c FROM orders'),
        'users': _count('SELECT COUNT(*) AS c FROM users'),
    })


# ─── Upload ảnh ─────────────────────────────────────────────────────────────────

@admin_bp.route('/upload/presign', methods=['POST'])
@_require_admin
def presign_image_url():
    """Trả presigned URL để hiển thị ảnh khi bucket không public (403)."""
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'url required'}), 400
    try:
        presigned = upload.presign_from_public_url(url)
        return jsonify({'url': presigned})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@admin_bp.route('/upload/image', methods=['POST'])
@_require_admin
def upload_image():
    """Upload ảnh lên MinIO, tự động convert WebP."""
    if 'file' not in request.files:
        return jsonify({'error': 'Không có file được upload'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Chưa chọn file'}), 400

    # Kiểm tra định dạng
    allowed = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed:
        return jsonify({'error': f'Định dạng không được hỗ trợ: {ext}'}), 400

    try:
        result = upload.upload_image(file)  # dùng folder từ config
        return jsonify({'success': True, 'url': result['url'], 'filename': result['filename']})
    except Exception as e:
        return jsonify({'error': f'Upload thất bại: {str(e)}'}), 500

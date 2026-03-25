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
        for field in ['features', 'images', 'screenshots']:
            try:
                p[field] = json.loads(p[field]) if p[field] else []
            except Exception:
                p[field] = []
        # alias installGuide cho frontend
        if 'install_guide' in p:
            p['installGuide'] = p['install_guide']
        if 'preview_audio' in p:
            p['previewAudio'] = p['preview_audio']
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
    content = data.get('content', '').strip() or None
    images = data.get('images', [])
    install_guide = data.get('installGuide', '').strip() or None
    screenshots = data.get('screenshots', [])

    if not name or not category or price <= 0:
        return jsonify({'error': 'name, category, price là bắt buộc'}), 400

    product_id = data.get('id', '').strip() or f"prod{uuid.uuid4().hex[:8]}"

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO products (id, name, category, price, original_price, badge, emoji, description, features, image, video_url, stock, is_active, content, images, install_guide, screenshots)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                content,
                json.dumps(images, ensure_ascii=False) if isinstance(images, list) else images,
                install_guide,
                json.dumps(screenshots, ensure_ascii=False) if isinstance(screenshots, list) else screenshots,
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
    for field in ['features', 'images', 'screenshots']:
        try:
            p[field] = json.loads(p[field]) if p[field] else []
        except Exception:
            p[field] = []
    if 'install_guide' in p:
        p['installGuide'] = p['install_guide']
    if 'preview_audio' in p:
        p['previewAudio'] = p['preview_audio']
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

    for field in ['name', 'category', 'price', 'original_price', 'badge', 'emoji', 'description', 'image', 'video_url', 'stock', 'is_active', 'content', 'images', 'screenshots', 'features', 'preview_audio']:
        if field in data:
            val = data[field]
            if field in ('price', 'original_price', 'stock'):
                val = int(val) if val else 0
            elif field == 'is_active':
                val = 1 if val else 0
            elif field in ('features', 'images', 'screenshots'):
                val = json.dumps(val, ensure_ascii=False) if isinstance(val, list) else val
            elif field == 'install_guide':
                continue
            elif isinstance(val, str):
                val = val.strip() or None
            updates.append(f'{field} = %s')
            params.append(val)

    # xử lý alias installGuide -> install_guide
    if 'installGuide' in data:
        val = data['installGuide']
        val = val.strip() if isinstance(val, str) else ''
        updates.append('install_guide = %s')
        params.append(val if val else None)
    # xử lý alias previewAudio -> preview_audio
    if 'previewAudio' in data:
        val = data['previewAudio']
        updates.append('preview_audio = %s')
        params.append(val if val else None)

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
            'licenseKey': r['license_key'] if r['status'] in ('completed', 'paid') else None,
            'key': r['license_key'] if r['status'] in ('completed', 'paid') else None,
            'createdAt': r['created_at'].isoformat() if r.get('created_at') else None,
            'date': r['created_at'],
        })
    return jsonify({'orders': orders})


@admin_bp.route('/orders/<order_id>/status', methods=['PUT'])
@_require_admin
def update_order_status(order_id):
    data = request.get_json() or {}
    new_status = data.get('status')

    valid = ('pending', 'completed', 'paid', 'cancelled')
    if new_status not in valid:
        return jsonify({'error': f'Status không hợp lệ. Chọn một trong: {valid}'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, status FROM orders WHERE id = %s', (order_id,))
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404

    cur.execute('SELECT id, status, product_id, license_key FROM orders WHERE id = %s', (order_id,))
    order_row = cur.fetchone()

    if new_status in ('completed', 'paid') and not order_row['license_key']:
        from orders import generate_license_key
        license_key = generate_license_key(order_row['product_id'][:2].upper())
        cur.execute(
            'UPDATE orders SET status = %s, license_key = %s, updated_at = NOW() WHERE id = %s',
            (new_status, license_key, order_id)
        )
        conn.commit()
        return jsonify({'success': True, 'id': order_id, 'status': new_status, 'licenseKey': license_key})
    else:
        cur.execute(
            'UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s',
            (new_status, order_id)
        )
        conn.commit()
        return jsonify({'success': True, 'id': order_id, 'status': new_status})


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


# ─── Preview Audio ────────────────────────────────────────────────────────────

@admin_bp.route('/products/<product_id>/preview', methods=['POST'])
@_require_admin
def upload_product_preview(product_id: str):
    """
    Upload file audio preview cho sản phẩm.
    File được lưu private trên MinIO, chỉ server stream được.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'Không có file được upload'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Chưa chọn file'}), 400

    # Kiểm tra sản phẩm tồn tại
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM products WHERE id = %s', (product_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    try:
        result = upload.upload_preview_audio(file, product_id)
        # Lưu đường dẫn vào DB
        cur.execute(
            'UPDATE products SET preview_audio = %s WHERE id = %s',
            (result['url'], product_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            'success': True,
            'url': result['url'],
            'filename': result['filename'],
            'message': f'Upload thành công. Endpoint: {result["url"]}'
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Upload thất bại: {str(e)}'}), 500


@admin_bp.route('/products/<product_id>/preview', methods=['DELETE'])
@_require_admin
def delete_product_preview(product_id: str):
    """Xóa preview audio của sản phẩm."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, preview_audio FROM products WHERE id = %s', (product_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    try:
        upload.delete_preview_audio(product_id)
        cur.execute('UPDATE products SET preview_audio = NULL WHERE id = %s', (product_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f'Xóa thất bại: {str(e)}'}), 500


# ─── Sounds ─────────────────────────────────────────────────────────────────

@admin_bp.route('/sounds', methods=['GET'])
@_require_admin
def list_sounds():
    """Danh sách tất cả âm thanh."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM sounds ORDER BY created_at DESC')
    rows = cur.fetchall()
    sounds = [dict(r) for r in rows]
    for s in sounds:
        s['id'] = s['id']
        s['isActive'] = bool(s.get('is_active', 1))
        if s.get('created_at'):
            s['createdAt'] = s['created_at'].isoformat() if hasattr(s['created_at'], 'isoformat') else str(s['created_at'])
    return jsonify({'sounds': sounds})


@admin_bp.route('/sounds', methods=['POST'])
@_require_admin
def create_sound():
    """Tạo record âm thanh + upload file lên MinIO."""
    if 'file' not in request.files:
        return jsonify({'error': 'Không có file được upload'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Chưa chọn file'}), 400

    name = request.form.get('name', '').strip()
    category = request.form.get('category', 'Sounds').strip() or 'Sounds'
    description = request.form.get('description', '').strip() or None
    duration_seconds = request.form.get('duration_seconds')

    if not name:
        return jsonify({'error': 'Tên âm thanh là bắt buộc'}), 400

    sound_id = request.form.get('id', '').strip() or f"sound{uuid.uuid4().hex[:8]}"

    conn = get_db()
    cur = conn.cursor()

    try:
        result = upload.upload_sound(file, sound_id)

        cur.execute(
            """
            INSERT INTO sounds (id, name, category, description, object_name, original_filename, file_size, duration_seconds, storage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                category = EXCLUDED.category,
                description = EXCLUDED.description,
                object_name = EXCLUDED.object_name,
                original_filename = EXCLUDED.original_filename,
                file_size = EXCLUDED.file_size,
                duration_seconds = EXCLUDED.duration_seconds,
                storage = EXCLUDED.storage
            """,
            (
                sound_id,
                name,
                category,
                description,
                result['object_name'],
                file.filename,
                result['size'],
                int(duration_seconds) if duration_seconds else None,
                result.get('storage', 'minio'),
            ),
        )
        conn.commit()

        cur.execute('SELECT * FROM sounds WHERE id = %s', (sound_id,))
        row = dict(cur.fetchone())
        row['isActive'] = bool(row.get('is_active', 1))
        if row.get('created_at'):
            row['createdAt'] = row['created_at'].isoformat() if hasattr(row['created_at'], 'isoformat') else str(row['created_at'])

        cur.close()
        conn.close()
        return jsonify({'success': True, 'sound': row}), 201

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': f'Tạo âm thanh thất bại: {str(e)}'}), 500


@admin_bp.route('/sounds/<sound_id>', methods=['PUT'])
@_require_admin
def update_sound(sound_id):
    """Cập nhật metadata âm thanh."""
    data = request.get_json() or {}

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM sounds WHERE id = %s', (sound_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({'error': 'Âm thanh không tồn tại'}), 404

    updates = []
    params = []
    for field in ['name', 'category', 'description', 'is_active', 'duration_seconds']:
        if field in data:
            val = data[field]
            if field == 'is_active':
                val = 1 if val else 0
            elif isinstance(val, str):
                val = val.strip() or None
            updates.append(f'{field} = %s')
            params.append(val)

    if not updates:
        return jsonify({'error': 'Không có thông tin cập nhật'}), 400

    params.append(sound_id)
    cur.execute(f"UPDATE sounds SET {', '.join(updates)} WHERE id = %s", params)
    conn.commit()

    cur.execute('SELECT * FROM sounds WHERE id = %s', (sound_id,))
    row = dict(cur.fetchone())
    row['isActive'] = bool(row.get('is_active', 1))
    if row.get('created_at'):
        row['createdAt'] = row['created_at'].isoformat() if hasattr(row['created_at'], 'isoformat') else str(row['created_at'])

    cur.close()
    conn.close()
    return jsonify({'success': True, 'sound': row})


@admin_bp.route('/sounds/<sound_id>', methods=['DELETE'])
@_require_admin
def delete_sound(sound_id):
    """Xóa âm thanh (xóa cả file MinIO)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM sounds WHERE id = %s', (sound_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Âm thanh không tồn tại'}), 404

    try:
        upload.delete_sound(row['object_name'], storage=row.get('storage') or 'minio')
    except Exception:
        pass

    cur.execute('DELETE FROM sounds WHERE id = %s', (sound_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'success': True})


@admin_bp.route('/sounds/<sound_id>/stream', methods=['GET'])
@_require_admin
def stream_sound_admin(sound_id):
    """Stream file âm thanh cho admin nghe thử."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT object_name, storage FROM sounds WHERE id = %s', (sound_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({'error': 'Âm thanh không tồn tại'}), 404

    try:
        resp, content_type = upload.stream_sound(
            row['object_name'], storage=row.get('storage') or 'minio'
        )
        from flask import Response
        return Response(resp.read(), mimetype=content_type)
    except ValueError:
        return jsonify({'error': 'File không tìm thấy trên kho lưu trữ'}), 404


@admin_bp.route('/sounds/<sound_id>/url', methods=['GET'])
@_require_admin
def sound_direct_url(sound_id):
    """
    Trả về URL trực tiếp để frontend chơi audio không qua server.
    Hỗ trợ cả MinIO (presigned) và OneDrive (downloadUrl).
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT object_name, storage FROM sounds WHERE id = %s', (sound_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({'error': 'Âm thanh không tồn tại'}), 404

    url = upload.get_sound_direct_url(row['object_name'], storage=row.get('storage') or 'minio')
    if not url:
        return jsonify({'error': 'Không lấy được URL trực tiếp'}), 404

    return jsonify({'url': url})


# ─── Deposit Management ─────────────────────────────────────────────────────────

@admin_bp.route('/deposits', methods=['GET'])
@_require_admin
def list_deposits():
    """Danh sách yêu cầu nạp tiền."""
    status_filter = request.args.get('status', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(per_page, 100)
    offset = (page - 1) * per_page

    conn = get_db()
    cur = conn.cursor()

    where = ''
    params: list = []
    if status_filter:
        where = 'WHERE d.status = %s'
        params.append(status_filter)

    cur.execute(f'SELECT COUNT(*) AS c FROM deposits d {where}', params)
    total_row = cur.fetchone()
    total = total_row['c'] if isinstance(total_row, dict) else total_row[0]

    cur.execute(f"""
        SELECT d.*, u.email AS user_email, u.name AS user_name
        FROM deposits d
        LEFT JOIN users u ON d.user_id = u.id
        {where}
        ORDER BY d.created_at DESC
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])
    rows = cur.fetchall()
    cur.close()

    return jsonify({
        'deposits': [
            {
                'id': r['id'],
                'userId': r['user_id'],
                'userEmail': r['user_email'],
                'userName': r['user_name'],
                'amount': r['amount'],
                'status': r['status'],
                'transferContent': r['transfer_content'],
                'bankName': r['bank_name'],
                'accountNumber': r['account_number'],
                'accountName': r['account_name'],
                'qrUrl': r['qr_url'],
                'createdAt': r['created_at'].isoformat() if r.get('created_at') else None,
                'confirmedAt': r['confirmed_at'].isoformat() if r.get('confirmed_at') else None,
                'cancelledAt': r['cancelled_at'].isoformat() if r.get('cancelled_at') else None,
            }
            for r in rows
        ],
        'pagination': {
            'page': page,
            'perPage': per_page,
            'total': total,
            'totalPages': (total + per_page - 1) // per_page,
        }
    })


@admin_bp.route('/deposits/<deposit_id>/approve', methods=['POST'])
@_require_admin
def admin_approve_deposit(deposit_id):
    """Admin duyệt nạp tiền - cộng tiền vào ví user."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM deposits WHERE id = %s AND status IN ('pending', 'confirmed')",
        (deposit_id,)
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Không tìm thấy hoặc đã xử lý'}), 404

    # Đảm bảo user có ví
    cur.execute('SELECT id FROM wallets WHERE user_id = %s', (row['user_id'],))
    wallet = cur.fetchone()
    if not wallet:
        wallet_id = str(uuid.uuid4())
        cur.execute(
            'INSERT INTO wallets (id, user_id, balance) VALUES (%s, %s, 0)',
            (wallet_id, row['user_id'])
        )
    else:
        wallet_id = wallet['id']

    # Cộng tiền
    cur.execute(
        "UPDATE wallets SET balance = balance + %s, updated_at = NOW() WHERE id = %s",
        (row['amount'], wallet_id)
    )
    cur.execute(
        "UPDATE deposits SET status = 'completed', confirmed_at = NOW() WHERE id = %s",
        (deposit_id,)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        'success': True,
        'message': f'Đã cộng {row["amount"]:,} VND vào ví của user',
    })


@admin_bp.route('/deposits/<deposit_id>/reject', methods=['POST'])
@_require_admin
def admin_reject_deposit(deposit_id):
    """Admin từ chối/hủy yêu cầu nạp tiền."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM deposits WHERE id = %s AND status IN ('pending', 'confirmed')",
        (deposit_id,)
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({'error': 'Không tìm thấy hoặc đã xử lý'}), 404

    cur.execute(
        "UPDATE deposits SET status = 'cancelled', cancelled_at = NOW() WHERE id = %s",
        (deposit_id,)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({'success': True, 'message': 'Đã hủy yêu cầu nạp tiền'})


@admin_bp.route('/wallets', methods=['GET'])
@_require_admin
def list_wallets():
    """Danh sách ví của tất cả users."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(per_page, 100)
    offset = (page - 1) * per_page

    conn = get_db()
    cur = conn.cursor()

    cur.execute('SELECT COUNT(*) AS c FROM wallets')
    row = cur.fetchone()
    total = row['c'] if isinstance(row, dict) else row[0]

    cur.execute("""
        SELECT w.*, u.email AS user_email, u.name AS user_name
        FROM wallets w
        LEFT JOIN users u ON w.user_id = u.id
        ORDER BY w.balance DESC
        LIMIT %s OFFSET %s
    """, [per_page, offset])
    rows = cur.fetchall()
    cur.close()

    return jsonify({
        'wallets': [
            {
                'id': r['id'],
                'userId': r['user_id'],
                'userEmail': r['user_email'],
                'userName': r['user_name'],
                'balance': r['balance'],
                'updatedAt': r['updated_at'].isoformat() if r.get('updated_at') else None,
            }
            for r in rows
        ],
        'pagination': {
            'page': page,
            'perPage': per_page,
            'total': total,
            'totalPages': (total + per_page - 1) // per_page,
        }
    })


@admin_bp.route('/wallets/<user_id>/adjust', methods=['POST'])
@_require_admin
def adjust_wallet_balance(user_id):
    """Admin đặt số dư ví theo giá trị nhập (set trực tiếp)."""
    data = request.get_json() or {}
    new_balance = data.get('balance', data.get('amount', None))
    note = data.get('note', '').strip()

    if not isinstance(new_balance, int) or new_balance < 0:
        return jsonify({'error': 'balance phải là số nguyên >= 0'}), 400

    conn = get_db()
    cur = conn.cursor()

    # Đảm bảo ví tồn tại
    cur.execute('SELECT id FROM wallets WHERE user_id = %s', (user_id,))
    wallet = cur.fetchone()
    if not wallet:
        wallet_id = str(uuid.uuid4())
        cur.execute(
            'INSERT INTO wallets (id, user_id, balance) VALUES (%s, %s, %s)',
            (wallet_id, user_id, new_balance)
        )
    else:
        wallet_id = wallet['id']
        cur.execute(
            'UPDATE wallets SET balance = %s, updated_at = NOW() WHERE id = %s',
            (new_balance, wallet_id)
        )

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        'success': True,
        'message': f'Đã cập nhật số dư mới: {new_balance:,} VND',
        'newBalance': new_balance,
        'note': note,
    })

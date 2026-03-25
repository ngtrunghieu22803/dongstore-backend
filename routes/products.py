from flask import Blueprint, request, jsonify, Response, url_for
import json
import os
import time
from threading import Lock
from typing import Dict, List, Tuple
from db import get_db
from upload import presign_from_public_url, stream_preview_audio, upload_preview_audio, stream_sound

products_bp = Blueprint('products', __name__)

# Base URL for absolute URLs in API responses
API_BASE_URL = os.environ.get('API_BASE_URL', '').rstrip('/')


def _abs_url(path: str) -> str:
    """Return absolute URL if API_BASE_URL is configured, else relative path."""
    if API_BASE_URL:
        return f"{API_BASE_URL}{path}"
    return path


# ─── In-memory rate limiter ─────────────────────────────────────────────────
# { ip: [(timestamp, product_id), ...] }
_RATE_STORE: Dict[str, List[Tuple[float, str]]] = {}
_RATE_LOCK = Lock()
RATE_LIMIT = 5          # lần nghe tối đa
RATE_WINDOW = 24 * 3600 # trong 24 giờ (giây)


def _rate_key() -> str:
    # Ưu tiên user_id nếu đăng nhập, không thì dùng IP
    from flask import session, has_request_context
    if has_request_context():
        uid = session.get('user_id', '')
        if uid:
            return f"u:{uid}"
        return request.remote_addr or 'unknown'
    return 'no-request'


def check_rate_limit(product_id: str) -> Tuple[bool, int]:
    """Kiểm tra rate limit. Returns (allowed, remaining)."""
    key = _rate_key()
    now = time.time()
    cutoff = now - RATE_WINDOW

    with _RATE_LOCK:
        entries = _RATE_STORE.get(key, [])
        # Lọc bỏ các entry cũ
        entries = [(ts, pid) for ts, pid in entries if ts > cutoff]
        _RATE_STORE[key] = entries

        # Đếm số lần nghe sản phẩm này
        count = sum(1 for ts, pid in entries if pid == product_id)
        if count >= RATE_LIMIT:
            return False, 0

        # Ghi nhận lượt nghe này
        entries.append((now, product_id))
        _RATE_STORE[key] = entries
        return True, RATE_LIMIT - count - 1


@products_bp.route('/<product_id>/preview', methods=['GET'])
def get_product_preview(product_id: str):
    """
    Stream file audio preview qua server (bảo mật — không lộ URL MinIO).
    Rate limit: 5 lần / 24h / IP hoặc user.
    """
    # Kiểm tra sản phẩm có tồn tại
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT preview_audio FROM products WHERE id = %s AND is_active = 1', (product_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({'error': 'Sản phẩm không tồn tại'}), 404

    if not row['preview_audio']:
        return jsonify({'error': 'Sản phẩm này chưa có audio preview'}), 404

    # Rate limit
    allowed, remaining = check_rate_limit(product_id)
    if not allowed:
        resp = jsonify({
            'error': 'Bạn đã nghe quá nhiều lần. Vui lòng thử lại sau 24 giờ hoặc mua sản phẩm.'
        })
        resp.status_code = 429
        resp.headers['Retry-After'] = str(RATE_WINDOW)
        return resp

    try:
        file_obj, content_type, filename = stream_preview_audio(product_id)
        resp = Response(
            file_obj,
            mimetype=content_type,
            headers={
                'Content-Disposition': f'inline; filename="{filename}"',
                'X-RateLimit-Remaining': str(remaining),
                'Cache-Control': 'no-store, no-cache, must-revalidate',
                'Pragma': 'no-cache',
            }
        )
        return resp
    except ValueError:
        return jsonify({'error': 'Preview audio không tìm thấy'}), 404
    except Exception as e:
        return jsonify({'error': f'Lỗi server: {e}'}), 500


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
        for field in ['features', 'images', 'screenshots']:
            try:
                p[field] = json.loads(p[field]) if p[field] else []
            except Exception:
                p[field] = []
        if 'install_guide' in p:
            p['installGuide'] = p['install_guide']
        if 'preview_audio' in p:
            p['previewAudio'] = p['preview_audio']
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


@products_bp.route('/categories', methods=['GET'])
def get_categories():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT category FROM products WHERE is_active = 1 ORDER BY category')
    rows = cur.fetchall()
    categories = ['Tất cả'] + [r['category'] for r in rows]
    return jsonify({'categories': categories})


@products_bp.route('/presign', methods=['POST'])
def presign_product_image():
    """Trả presigned URL để hiển thị ảnh MinIO (không cần auth)."""
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'url required'}), 400
    try:
        presigned = presign_from_public_url(url)
        return jsonify({'url': presigned})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ─── Public Sounds ───────────────────────────────────────────────────────────

@products_bp.route('/sounds', methods=['GET'])
def get_sounds():
    """Danh sách âm thanh cho trang kho âm thanh (không cần auth)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM sounds WHERE is_active = 1 ORDER BY created_at DESC')
    rows = cur.fetchall()
    sounds = [dict(r) for r in rows]
    for s in sounds:
        s['id'] = s['id']
        s['streamUrl'] = _abs_url(f'/api/products/sounds/{s["id"]}/stream')
        if s.get('created_at'):
            s['createdAt'] = s['created_at'].isoformat() if hasattr(s['created_at'], 'isoformat') else str(s['created_at'])
    return jsonify({'sounds': sounds})


@products_bp.route('/sounds/<sound_id>/stream', methods=['GET'])
def stream_sound_public(sound_id: str):
    """Stream file âm thanh cho user nghe thử (không cần auth, có rate limit)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT object_name, storage FROM sounds WHERE id = %s AND is_active = 1',
        (sound_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({'error': 'Âm thanh không tồn tại'}), 404

    # Rate limit nghe thử (dùng sound_id thay vì product_id)
    allowed, _ = check_rate_limit(f'sound:{sound_id}')
    if not allowed:
        return jsonify({
            'error': 'Bạn đã nghe quá nhiều lần. Vui lòng mua sản phẩm để tải file gốc.'
        }), 429

    try:
        resp, content_type = stream_sound(
            row['object_name'], storage=row.get('storage') or 'minio'
        )
        return Response(resp.read(), mimetype=content_type)
    except ValueError:
        return jsonify({'error': 'File không tìm thấy'}), 404

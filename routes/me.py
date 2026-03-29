"""API /api/me — dữ liệu theo user đăng nhập (JWT)."""
from flask import Blueprint, jsonify, request
from db import get_db
from auth import require_auth

me_bp = Blueprint('me', __name__)


@me_bp.route('/sound-imports', methods=['GET'])
@require_auth
def get_my_sound_imports():
    """Danh sách product_id âm thanh đã xác nhận thêm vào app (từ Electron)."""
    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT product_id FROM user_sound_imports
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        product_ids = [r['product_id'] for r in rows]
        return jsonify({'productIds': product_ids})
    finally:
        cur.close()
        conn.close()

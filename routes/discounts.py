from typing import Optional
from flask import Blueprint, request, jsonify
from datetime import datetime, timezone
from db import get_db
from auth import require_auth
from rate_limit import hit_rate, response_429

discounts_bp = Blueprint('discounts', __name__)


def _get_discount_by_code(code: str, product_id: Optional[str] = None):
    conn = get_db()
    cur = conn.cursor()
    try:
        if product_id:
            cur.execute(
                """
                SELECT *
                FROM discount_codes
                WHERE code = %s AND (product_id IS NULL OR product_id = %s)
                ORDER BY product_id NULLS FIRST
                LIMIT 1
                """,
                (code, product_id),
            )
        else:
            cur.execute(
                """
                SELECT *
                FROM discount_codes
                WHERE code = %s
                ORDER BY product_id NULLS FIRST
                LIMIT 1
                """,
                (code,),
            )

        row = cur.fetchone()
        if not row:
            return None

        # used_count luôn lấy theo đơn đã hoàn thành để tránh lệch dữ liệu lịch sử.
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE discount_code = %s
              AND status IN ('completed', 'paid')
            """,
            (row["code"],),
        )
        used_row = cur.fetchone()
        real_used = int((used_row or {}).get("c") or 0)
        if isinstance(row, dict):
            row["used_count"] = real_used
        return row
    finally:
        cur.close()
        conn.close()


def _validate_discount_row(row, order_total: int):
    now = datetime.now(timezone.utc)
    if not row:
        return False, "Mã giảm giá không tồn tại"
    status = (row.get("status") or "").upper()
    if status != "ACTIVE":
        return False, "Mã giảm giá đã bị vô hiệu hóa"
    vf = row.get("valid_from")
    vt = row.get("valid_to")
    if vf and hasattr(vf, "timestamp") and vf > now:
        return False, "Mã giảm giá chưa đến thời gian sử dụng"
    if vt and hasattr(vt, "timestamp") and vt < now:
        return False, "Mã giảm giá đã hết hạn"
    max_uses = row.get("max_uses")
    used = row.get("used_count") or 0
    if max_uses is not None and used >= max_uses:
        return False, "Mã giảm giá đã được sử dụng tối đa số lần"
    min_amount = row.get("min_order_amount") or 0
    if order_total < min_amount:
        return False, "Đơn hàng chưa đủ giá trị tối thiểu để áp dụng mã"
    return True, None


def _calc_discount_amount(row, order_total: int) -> int:
    dtype = (row.get("discount_type") or "PERCENT").upper()
    value = int(row.get("value") or 0)
    if value <= 0:
        return 0
    if dtype == "FIXED":
        return min(value, order_total)
    # PERCENT
    return max(int(order_total * value / 100), 0)


def _has_user_used_discount(user_id: Optional[str], exclude_order_id: Optional[str] = None) -> bool:
    if not user_id:
        return False
    conn = get_db()
    cur = conn.cursor()
    try:
        sql = """
            SELECT 1
            FROM orders
            WHERE user_id = %s
              AND discount_code IS NOT NULL
              AND status IN ('completed', 'paid')
        """
        params = [user_id]
        if exclude_order_id:
            sql += " AND id <> %s"
            params.append(exclude_order_id)
        sql += " LIMIT 1"
        cur.execute(sql, tuple(params))
        return bool(cur.fetchone())
    finally:
        cur.close()
        conn.close()


@discounts_bp.route("/validate", methods=["POST"])
@require_auth
def validate_discount():
    # 40 req / phút / user — chống dò mã
    if not hit_rate("discount_validate", 40, 60, request.user["id"]):
        return response_429(60)
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    order_total = int(data.get("orderTotal") or 0)
    product_id = (data.get("productId") or data.get("product_id") or "").strip() or None
    if not code:
        return jsonify({"error": "code là bắt buộc"}), 400
    if order_total <= 0:
        return jsonify({"error": "orderTotal phải lớn hơn 0"}), 400

    if _has_user_used_discount(request.user["id"]):
        return jsonify({"error": "Mỗi tài khoản chỉ được sử dụng 1 mã giảm giá"}), 400

    row = _get_discount_by_code(code, product_id)
    ok, msg = _validate_discount_row(row, order_total)
    if not ok:
        return jsonify({"error": msg}), 400

    discount_amount = _calc_discount_amount(row, order_total)
    final_total = max(order_total - discount_amount, 0)

    return jsonify(
        {
            "code": row["code"],
            "discountType": row["discount_type"],
            "value": row["value"],
            "discountAmount": discount_amount,
            "finalTotal": final_total,
        }
    )


def apply_discount_if_any(
    raw_code,
    base_total: int,
    product_id: Optional[str] = None,
    user_id: Optional[str] = None,
    exclude_order_id: Optional[str] = None,
):
    """
    Dùng trong routes.orders/payments:
      - raw_code: chuỗi mã từ body (có thể None/'')
      - base_total: tổng giá gốc
    Trả về (final_total, discount_code, discount_amount, error_message)
      - error_message != None nếu mã không hợp lệ.
    """
    code = (raw_code or "").strip()
    if not code:
        return base_total, None, 0, None
    if _has_user_used_discount(user_id, exclude_order_id=exclude_order_id):
        return base_total, None, 0, "Mỗi tài khoản chỉ được sử dụng 1 mã giảm giá"
    row = _get_discount_by_code(code, product_id)
    ok, msg = _validate_discount_row(row, base_total)
    if not ok:
        return base_total, None, 0, msg
    discount_amount = _calc_discount_amount(row, base_total)
    final_total = max(base_total - discount_amount, 0)
    return final_total, row["code"], discount_amount, None


def mark_discount_used_for_order(order_id: str) -> None:
    """
    Tăng used_count đúng 1 lần cho mã giảm giá của order đã completed/paid.
    Dùng cờ orders.discount_counted để chống cộng trùng.
    """
    if not order_id:
        return
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            WITH upd AS (
                UPDATE orders
                SET discount_counted = 1, updated_at = NOW()
                WHERE id = %s
                  AND discount_code IS NOT NULL
                  AND COALESCE(discount_counted, 0) = 0
                  AND status IN ('completed', 'paid')
                RETURNING discount_code
            )
            UPDATE discount_codes d
            SET used_count = COALESCE(d.used_count, 0) + 1,
                updated_at = NOW()
            FROM upd
            WHERE d.code = upd.discount_code
            """,
            (order_id,),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


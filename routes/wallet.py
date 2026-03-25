from flask import Blueprint, request, jsonify, current_app
import uuid
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests
from db import get_db
from auth import require_auth

wallet_bp = Blueprint('wallet', __name__)


def _get_bank_name(bank_id: str) -> str:
    bank_map = {
        '970422': 'MB Bank',
        '970448': 'TPBank',
        '970436': 'Vietcombank',
        '970405': 'Citibank',
        '970431': 'VietinBank',
        '970418': 'VPBank',
        '970437': 'Sacombank',
        '970432': 'BIDV',
    }
    return bank_map.get(bank_id, f'Ngân hàng ({bank_id})')


def _ensure_wallet(user_id: str) -> str:
    """Tạo ví nếu chưa có, trả về wallet_id."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM wallets WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if row:
        wallet_id = row['id']
    else:
        wallet_id = str(uuid.uuid4())
        cur.execute(
            'INSERT INTO wallets (id, user_id, balance) VALUES (%s, %s, 0)',
            (wallet_id, user_id)
        )
        conn.commit()
    cur.close()
    return wallet_id


def _cancel_expired_deposits() -> int:
    """Hủy các deposit pending quá 30 phút."""
    conn = get_db()
    cur = conn.cursor()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    cur.execute(
        "UPDATE deposits SET status = 'cancelled', cancelled_at = NOW() "
        "WHERE status = 'pending' AND created_at < %s",
        (cutoff,)
    )
    conn.commit()
    n = cur.rowcount
    if n > 0:
        print(f"[WALLET] Auto-cancelled {n} expired deposit(s)")
    return n


# ─── GET /api/wallet/balance ──────────────────────────────────────────────────
@wallet_bp.route('/balance', methods=['GET'])
@require_auth
def get_balance():
    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT balance FROM wallets WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    balance = row['balance'] if row else 0
    cur.close()
    return jsonify({'balance': balance})


# ─── POST /api/wallet/deposit ─────────────────────────────────────────────────
@wallet_bp.route('/deposit', methods=['POST'])
@require_auth
def create_deposit():
    _cancel_expired_deposits()
    data = request.get_json() or {}
    amount = data.get('amount')

    if not amount or not isinstance(amount, int) or amount < 10000:
        return jsonify({'error': 'Số tiền tối thiểu là 10,000 VND'}), 400
    if amount > 500_000_000:
        return jsonify({'error': 'Số tiền tối đa là 500,000,000 VND'}), 400

    user_id = request.user['id']
    deposit_id = f"ND{uuid.uuid4().hex[:10].upper()}"
    transfer_content = f"NAP{deposit_id}"

    bank_id = current_app.config.get('BANK_ID', '970422')
    account_number = current_app.config.get('BANK_ACCOUNT_NUMBER', '')
    account_name = current_app.config.get('BANK_ACCOUNT_NAME', '')

    if not account_number:
        return jsonify({'error': 'Thông tin tài khoản ngân hàng chưa được cấu hình'}), 500

    name_safe = ''.join(c for c in account_name.strip().upper() if c.isascii())
    qr_url = (
        f"https://api.vietqr.io/image/{bank_id}-{account_number}-compact2.jpg"
        f"?accountName={urllib.parse.quote(name_safe, safe='')}"
        f"&amount={amount}"
        f"&addInfo={urllib.parse.quote(transfer_content, safe='')}"
    )

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO deposits (id, user_id, amount, transfer_content, bank_name, account_number, account_name, qr_url, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
    """, (deposit_id, user_id, amount, transfer_content, _get_bank_name(bank_id), account_number, account_name, qr_url))
    conn.commit()
    cur.close()

    return jsonify({
        'depositId': deposit_id,
        'amount': amount,
        'transferContent': transfer_content,
        'bankName': _get_bank_name(bank_id),
        'accountNumber': account_number,
        'accountName': account_name,
        'qrUrl': qr_url,
    }), 201


# ─── GET /api/wallet/deposit/<id> ────────────────────────────────────────────
@wallet_bp.route('/deposit/<deposit_id>', methods=['GET'])
@require_auth
def get_deposit(deposit_id):
    _cancel_expired_deposits()
    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM deposits WHERE id = %s AND user_id = %s",
        (deposit_id, user_id)
    )
    row = cur.fetchone()
    cur.close()

    if not row:
        return jsonify({'error': 'Không tìm thấy yêu cầu nạp tiền'}), 404

    return jsonify({
        'id': row['id'],
        'amount': row['amount'],
        'status': row['status'],
        'transferContent': row['transfer_content'],
        'bankName': row['bank_name'],
        'accountNumber': row['account_number'],
        'accountName': row['account_name'],
        'qrUrl': row['qr_url'],
        'createdAt': row['created_at'],
        'confirmedAt': row['confirmed_at'],
    })


# ─── POST /api/wallet/deposit/<id>/confirm ───────────────────────────────────
@wallet_bp.route('/deposit/<deposit_id>/confirm', methods=['POST'])
@require_auth
def confirm_deposit(deposit_id):
    """User xác nhận đã chuyển khoản → admin sẽ duyệt trong trang admin."""
    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM deposits WHERE id = %s AND user_id = %s AND status = 'pending'",
        (deposit_id, user_id)
    )
    row = cur.fetchone()

    if not row:
        return jsonify({'error': 'Không tìm thấy hoặc đã xử lý'}), 404

    # Đánh dấu đã xác nhận (chờ admin duyệt)
    cur.execute(
        "UPDATE deposits SET status = 'confirmed', confirmed_at = NOW() WHERE id = %s",
        (deposit_id,)
    )
    conn.commit()
    cur.close()

    return jsonify({
        'success': True,
        'message': 'Đã xác nhận. Vui lòng chờ admin duyệt trong vài phút.',
    })


# ─── POST /api/wallet/deposit/<id>/approve ────────────────────────────────────
@wallet_bp.route('/deposit/<deposit_id>/approve', methods=['POST'])
@require_auth
def approve_deposit(deposit_id):
    """Admin duyệt nạp tiền - cộng tiền vào ví."""
    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()

    # Check admin
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Không có quyền'}), 403

    cur.execute(
        "SELECT * FROM deposits WHERE id = %s AND status IN ('pending', 'confirmed')",
        (deposit_id,)
    )
    row = cur.fetchone()
    if not row:
        return jsonify({'error': 'Không tìm thấy hoặc đã xử lý'}), 404

    # Cộng tiền vào ví
    wallet_id = _ensure_wallet(row['user_id'])
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

    return jsonify({
        'success': True,
        'message': f'Đã cộng {row["amount"]:,} VND vào ví của user',
    })


# ─── GET /api/wallet/deposits ─────────────────────────────────────────────────
@wallet_bp.route('/deposits', methods=['GET'])
@require_auth
def get_deposits():
    user_id = request.user['id']
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(per_page, 100)
    offset = (page - 1) * per_page

    conn = get_db()
    cur = conn.cursor()

    cur.execute('SELECT COUNT(*) FROM deposits WHERE user_id = %s', (user_id,))
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT id, amount, status, transfer_content, created_at, confirmed_at
        FROM deposits
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """, (user_id, per_page, offset))
    rows = cur.fetchall()
    cur.close()

    return jsonify({
        'deposits': [
            {
                'id': r['id'],
                'amount': r['amount'],
                'status': r['status'],
                'transferContent': r['transfer_content'],
                'createdAt': r['created_at'],
                'confirmedAt': r['confirmed_at'],
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


# ─── POST /api/wallet/pay ──────────────────────────────────────────────────────
@wallet_bp.route('/pay', methods=['POST'])
@require_auth
def pay_with_wallet():
    data = request.get_json() or {}
    order_id = data.get('orderId')

    if not order_id:
        return jsonify({'error': 'orderId là bắt buộc'}), 400

    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()

    # Lấy đơn hàng
    cur.execute(
        "SELECT * FROM orders WHERE id = %s AND user_id = %s",
        (order_id, user_id)
    )
    order = cur.fetchone()
    if not order:
        return jsonify({'error': 'Đơn hàng không tồn tại'}), 404
    if order['status'] != 'pending':
        return jsonify({'error': 'Đơn hàng không ở trạng thái chờ thanh toán'}), 400

    # Lấy số dư
    _ensure_wallet(user_id)
    cur.execute('SELECT balance FROM wallets WHERE user_id = %s', (user_id,))
    wallet = cur.fetchone()
    balance = wallet['balance'] if wallet else 0
    order_price = order['total_price']

    if balance < order_price:
        return jsonify({
            'error': 'Số dư không đủ',
            'required': order_price,
            'current': balance,
            'shortage': order_price - balance,
        }), 400

    # Tạo license key
    license_key = f"{order['product_id'][:2].upper()}-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[8:16].upper()}"

    # Transaction: trừ tiền + cập nhật đơn hàng
    cur.execute(
        "UPDATE wallets SET balance = balance - %s, updated_at = NOW() WHERE user_id = %s",
        (order_price, user_id)
    )
    cur.execute("""
        UPDATE orders
        SET status = 'completed', license_key = %s, payment_method = 'wallet', updated_at = NOW()
        WHERE id = %s
    """, (license_key, order_id))
    conn.commit()
    cur.close()

    return jsonify({
        'success': True,
        'licenseKey': license_key,
        'remainingBalance': balance - order_price,
        'order': {
            'id': order_id,
            'price': order_price,
            'status': 'completed',
        }
    })

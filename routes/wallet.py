import time
import secrets
from flask import Blueprint, request, jsonify, current_app
import uuid
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
import requests
from typing import Optional
from db import get_db
from auth import require_auth
from crypto import hash_license_secret
from routes.orders import normalize_license_display_title, is_sound_product
from rate_limit import hit_rate, response_429

wallet_bp = Blueprint('wallet', __name__)

# Đổi ID máy (HWID) — xóa gắn để kích hoạt lại máy khác
HWID_RESET_PRICE = 10_000
FREE_HWID_RESET_DELTA = timedelta(hours=24)


def _dt_utc(dt):
    if dt is None:
        return None
    if getattr(dt, 'tzinfo', None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ─── Thueapibank.vn auto-check config ─────────────────────────────────────────
THUEAPIBANK_PASSWORD = None
THUEAPIBANK_API_BASE = 'https://thueapibank.vn'
BANK_ACCOUNT_NUMBER = None  # Số tài khoản Vietcombank
THUEAPIBANK_TOKEN = None


def _reload_thueapibank_config():
    global THUEAPIBANK_PASSWORD, THUEAPIBANK_API_BASE, BANK_ACCOUNT_NUMBER, THUEAPIBANK_TOKEN
    THUEAPIBANK_PASSWORD = current_app.config.get('THUEAPIBANK_PASSWORD', '')
    THUEAPIBANK_API_BASE = current_app.config.get('THUEAPIBANK_API_BASE', 'https://thueapibank.vn')
    BANK_ACCOUNT_NUMBER = current_app.config.get('BANK_ACCOUNT_NUMBER', '')
    THUEAPIBANK_TOKEN = current_app.config.get('THUEAPIBANK_TOKEN', '')


def _check_thueapibank_transfer(user_id: str, expected_amount: int, transfer_content: str) -> dict:
    """
    Kiểm tra giao dịch qua thueapibank.vn API.
    Trả về {'found': True, 'amount': xxx} nếu có giao dịch khớp,
    hoặc {'found': False} nếu không.
    """
    if not THUEAPIBANK_PASSWORD or not THUEAPIBANK_TOKEN:
        return {'found': False, 'error': 'thueapibank chưa cấu hình'}

    try:
        url = f"{THUEAPIBANK_API_BASE}/historyapivcbv3/{THUEAPIBANK_PASSWORD}/{BANK_ACCOUNT_NUMBER}/{THUEAPIBANK_TOKEN}"
        print(f"[WALLET CHECK] 📡 GET {url}")
        resp = requests.get(url)
        if resp.status_code != 200:
            return {'found': False, 'error': f'API trả về HTTP {resp.status_code}, có thể URL sai hoặc credentials hết hạn'}
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return {'found': False, 'error': 'API trả về HTML thay vì JSON — kiểm tra URL/credentials'}

        if data.get('status') != 'success':
            return {'found': False, 'error': data.get('message', 'API lỗi')}

        for tx in data.get('transactions', []):
            if tx.get('type') != 'IN':
                continue
            if int(tx.get('amount', 0)) != expected_amount:
                continue
            # Kiểm tra nội dung chuyển tiền có chứa transfer_content
            desc = tx.get('description', '')
            if transfer_content.upper() in desc.upper():
                return {
                    'found': True,
                    'amount': tx['amount'],
                    'description': desc,
                    'transactionDate': tx.get('transactionDate'),
                    'transactionID': tx.get('transactionID'),
                }
        return {'found': False}
    except Exception as e:
        return {'found': False, 'error': str(e)}


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


KEY_DURATION_DAYS = {
    '3_days': 3,
    '1_month': 30,
    '3_months': 90,
    '1_year': 365,
}


def _expires_at_from_duration(duration_code: Optional[str]):
    if not duration_code:
        return None
    days = KEY_DURATION_DAYS.get(duration_code)
    if not days:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


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


def _auto_check_pending_deposits() -> int:
    """
    Background job: kiểm tra các deposit pending.
    - APScheduler chạy ngoài request context → app truyền vào từ app.py.
    - Lấy danh sách deposit pending trong 10 phút.
    - Gọi thueapibank API kiểm tra giao dịch.
    - Nếu tìm thấy → tự động duyệt (cộng tiền vào ví).
    - Mỗi lần chạy đều log ra console.
    """
    def _run_check() -> int:
        _reload_thueapibank_config()
        conn = get_db()
        cur = conn.cursor()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        cur.execute("""
            SELECT id, user_id, amount, transfer_content, created_at
            FROM deposits
            WHERE status = 'pending' AND created_at >= %s
            ORDER BY created_at ASC
        """, (cutoff,))
        pending = cur.fetchall()
        cur.close()

        if not pending:
            return 0

        deposit_ids = [p['id'] for p in pending]
        print(f"[WALLET CHECK] 🔍 Checking {len(pending)} deposit(s): {deposit_ids}")

        url = f"{THUEAPIBANK_API_BASE}/historyapivcbv3/{THUEAPIBANK_PASSWORD}/{BANK_ACCOUNT_NUMBER}/{THUEAPIBANK_TOKEN}"
        print(f"[WALLET CHECK] 📡 GET {url}")

        try:
            resp = requests.get(url)
            print(f"[WALLET CHECK] 📥 HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(f"[WALLET CHECK] ❌ API trả về HTTP {resp.status_code}")
                return 0
            try:
                data = resp.json()
            except json.JSONDecodeError:
                print(f"[WALLET CHECK] ❌ API trả về HTML thay vì JSON")
                return 0

            if data.get('status') != 'success':
                print(f"[WALLET CHECK] ❌ API returned: {data.get('message', 'lỗi')}")
                return 0

            txns = data.get('transactions', [])
            print(f"[WALLET CHECK] 📥 Received {len(txns)} transaction(s) from bank")

            confirmed = 0
            for dep in pending:
                dep_id = dep['id']
                user_id = dep['user_id']
                amount = dep['amount']
                transfer_content = dep['transfer_content']

                for tx in txns:
                    if tx.get('type') != 'IN':
                        continue
                    if int(tx.get('amount', 0)) != amount:
                        continue
                    desc = tx.get('description', '')
                    if transfer_content.upper() not in desc.upper():
                        continue

                    print(f"[WALLET CHECK] ✅ MATCH! Deposit {dep_id} — {amount:,}đ — \"{desc}\"")

                    conn2 = get_db()
                    cur2 = conn2.cursor()
                    wallet_id = _ensure_wallet(user_id)
                    cur2.execute(
                        "UPDATE wallets SET balance = balance + %s, updated_at = NOW() WHERE id = %s",
                        (amount, wallet_id)
                    )
                    cur2.execute(
                        "UPDATE deposits SET status = 'completed', confirmed_at = NOW() WHERE id = %s",
                        (dep_id,)
                    )
                    conn2.commit()
                    cur2.close()
                    conn2.close()

                    confirmed += 1
                    break

            if confirmed > 0:
                print(f"[WALLET CHECK] 🎉 Auto-confirmed {confirmed} deposit(s)")
            return confirmed

        except json.JSONDecodeError:
            print(f"[WALLET CHECK] ❌ API trả về HTML (HTTP {resp.status_code}) thay vì JSON — có thể URL sai hoặc API đã thay đổi: {resp.text[:200]}")
            return 0
        except Exception as e:
            print(f"[WALLET CHECK] ❌ Exception: {e}")
            return 0

    return _run_check


def create_auto_check_job(app):
    """
    Factory: nhận app instance, trả về job function có app bound.
    Dùng cho APScheduler — đăng ký trong app.py sau khi app đã tạo.
    """
    def _job() -> int:
        with app.app_context():
            _reload_thueapibank_config()
            conn = get_db()
            cur = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            cur.execute("""
                SELECT id, user_id, amount, transfer_content, created_at
                FROM deposits
                WHERE status = 'pending' AND created_at >= %s
                ORDER BY created_at ASC
            """, (cutoff,))
            pending = cur.fetchall()
            cur.close()

            if not pending:
                return 0

            deposit_ids = [p['id'] for p in pending]
            print(f"[WALLET CHECK] 🔍 Checking {len(pending)} deposit(s): {deposit_ids}")

            url = f"{THUEAPIBANK_API_BASE}/historyapivcbv3/{THUEAPIBANK_PASSWORD}/{BANK_ACCOUNT_NUMBER}/{THUEAPIBANK_TOKEN}"
            print(f"[WALLET CHECK] 📡 GET {url}")

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'application/json',
                }
                resp = requests.get(url)
                if resp.status_code != 200:
                    print(f"[WALLET CHECK] ❌ API trả về HTTP {resp.status_code}")
                    return 0
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    print(f"[WALLET CHECK] ❌ API trả về HTML thay vì JSON")
                    return 0

                if data.get('status') != 'success':
                    print(f"[WALLET CHECK] ❌ API returned: {data.get('message', 'lỗi')}")
                    return 0

                txns = data.get('transactions', [])
                print(f"[WALLET CHECK] 📥 Received {len(txns)} transaction(s) from bank")

                confirmed = 0
                for dep in pending:
                    dep_id = dep['id']
                    user_id = dep['user_id']
                    amount = dep['amount']
                    transfer_content = dep['transfer_content']

                    for tx in txns:
                        if tx.get('type') != 'IN':
                            continue
                        if int(tx.get('amount', 0)) != amount:
                            continue
                        desc = tx.get('description', '')
                        if transfer_content.upper() not in desc.upper():
                            continue

                        print(f"[WALLET CHECK] ✅ MATCH! Deposit {dep_id} — {amount:,}đ — \"{desc}\"")

                        conn2 = get_db()
                        cur2 = conn2.cursor()
                        wallet_id = _ensure_wallet(user_id)
                        cur2.execute(
                            "UPDATE wallets SET balance = balance + %s, updated_at = NOW() WHERE id = %s",
                            (amount, wallet_id)
                        )
                        cur2.execute(
                            "UPDATE deposits SET status = 'completed', confirmed_at = NOW() WHERE id = %s",
                            (dep_id,)
                        )
                        conn2.commit()
                        cur2.close()
                        conn2.close()

                        confirmed += 1
                        break

                if confirmed > 0:
                    print(f"[WALLET CHECK] 🎉 Auto-confirmed {confirmed} deposit(s)")
                return confirmed

            except json.JSONDecodeError:
                print(f"[WALLET CHECK] ❌ API trả về HTML (HTTP {resp.status_code}) thay vì JSON — có thể URL sai hoặc API đã thay đổi: {resp.text[:200]}")
                return 0
            except Exception as e:
                print(f"[WALLET CHECK] ❌ Exception: {e}")
                return 0

    return _job


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
    if not hit_rate("wallet_deposit", 15, 300, request.user["id"]):
        return response_429(300)
    _cancel_expired_deposits()
    _reload_thueapibank_config()
    data = request.get_json() or {}
    amount = data.get('amount')

    if not amount or not isinstance(amount, int) or amount < 10000:
        return jsonify({'error': 'Số tiền tối thiểu là 10,000 VND'}), 400
    if amount > 500_000_000:
        return jsonify({'error': 'Số tiền tối đa là 500,000,000 VND'}), 400

    user_id = request.user['id']
    deposit_id = f"ND{uuid.uuid4().hex[:10].upper()}"
    transfer_content = f"NAP{deposit_id}"

    # VietQR Quick Link config
    vietqr_template = current_app.config.get('VIETQR_TEMPLATE', 'UH8kzQZ')
    bank_id = current_app.config.get('BANK_ID', '970436')  # Vietcombank mặc định
    account_number = current_app.config.get('BANK_ACCOUNT_NUMBER', '')
    account_name = current_app.config.get('BANK_ACCOUNT_NAME', '')

    if not account_number:
        return jsonify({'error': 'Thông tin tài khoản ngân hàng chưa được cấu hình'}), 500

    name_safe = ''.join(c for c in account_name.strip().upper() if c.isascii())
    # Dùng Quick Link với Template ID
    qr_url = (
        f"https://api.vietqr.io/image/{bank_id}-{account_number}-{vietqr_template}.jpg"
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
    """User xác nhận đã chuyển khoản → tự động kiểm tra qua thueapibank.vn."""
    if not hit_rate("wallet_deposit_confirm", 25, 300, request.user["id"]):
        return response_429(300)
    _reload_thueapibank_config()
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

    # Tự động kiểm tra thueapibank
    check = _check_thueapibank_transfer(
        user_id,
        row['amount'],
        row['transfer_content']
    )

    if check.get('found'):
        # Tìm thấy giao dịch → tự động duyệt
        wallet_id = _ensure_wallet(user_id)
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
            'autoConfirmed': True,
            'message': f'Tự động xác nhận! Đã cộng {row["amount"]:,} VND vào ví.',
            'transaction': check,
        })

    # Không tìm thấy → chờ admin duyệt
    cur.execute(
        "UPDATE deposits SET status = 'confirmed', confirmed_at = NOW() WHERE id = %s",
        (deposit_id,)
    )
    conn.commit()
    cur.close()

    return jsonify({
        'success': True,
        'autoConfirmed': False,
        'message': 'Không tìm thấy giao dịch. Vui lòng chờ admin duyệt hoặc kiểm tra lại.',
        'checkResult': check,
    })


# ─── POST /api/wallet/deposit/<id>/check ─────────────────────────────────────
@wallet_bp.route('/deposit/<deposit_id>/check', methods=['POST'])
@require_auth
def check_deposit(deposit_id):
    """Endpoint riêng để kiểm tra giao dịch qua thueapibank (gọi thủ công)."""
    if not hit_rate("wallet_deposit_check", 40, 60, request.user["id"]):
        return response_429(60)
    _reload_thueapibank_config()
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

    check = _check_thueapibank_transfer(
        user_id,
        row['amount'],
        row['transfer_content']
    )

    return jsonify({
        'depositId': deposit_id,
        'amount': row['amount'],
        'transferContent': row['transfer_content'],
        'status': row['status'],
        'checkResult': check,
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

    cur.execute('SELECT COUNT(*) as total FROM deposits WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    total = row['total'] if row else 0

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
    if not hit_rate("wallet_pay", 20, 300, request.user["id"]):
        return response_429(300)
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

    duration_code = None
    renew_key = None
    display_title = None
    try:
        snap = json.loads(order.get('product_snapshot') or '{}')
        duration_code = snap.get('duration_code')
        renew_key = (snap.get('renew_key') or '').strip() or None
        display_title = normalize_license_display_title(snap.get('display_title'))
    except Exception:
        duration_code = None
        renew_key = None
        display_title = None

    cur.execute('SELECT * FROM products WHERE id = %s', (order['product_id'],))
    product_row = cur.fetchone()

    # Sản phẩm tải miễn phí sau khi nạp tiền: không cho thanh toán để tạo key.
    # Giữ backend nhất quán với frontend (button đổi sang “Tải”, không sinh license_key).
    if product_row and bool(product_row.get('require_first_deposit')):
        return jsonify({
            'error': 'Sản phẩm này chỉ hỗ trợ tải miễn phí sau khi nạp tiền; không thanh toán để tạo key.'
        }), 400

    if renew_key:
        cur.execute('SELECT require_duration FROM products WHERE id = %s', (order['product_id'],))
        prow = cur.fetchone()
        if not prow or not bool(prow.get('require_duration')):
            return jsonify({'error': 'Sản phẩm này không hỗ trợ gia hạn'}), 400
        cur.execute(
            "SELECT id, license_key, license_secret_hash, product_id, user_id, expires_at, status FROM licenses WHERE license_key = %s",
            (renew_key,),
        )
        old = cur.fetchone()
        if not old:
            return jsonify({'error': 'Key cần gia hạn không tồn tại'}), 400
        if old.get('user_id') != user_id or old.get('product_id') != order['product_id']:
            return jsonify({'error': 'Key gia hạn không hợp lệ'}), 400
        if (old.get('status') or 'active') != 'active':
            return jsonify({'error': 'Key đã bị vô hiệu hóa, không thể gia hạn'}), 400

        days = KEY_DURATION_DAYS.get(duration_code or '', 0)
        base = old['expires_at'] if old.get('expires_at') and old['expires_at'] > datetime.now(timezone.utc) else datetime.now(timezone.utc)
        expires_at = base + timedelta(days=days) if days > 0 else old.get('expires_at')

        license_key = old['license_key']
        cur.execute(
            "UPDATE licenses SET expires_at = %s, status = 'active', activated_at = NOW() WHERE license_key = %s",
            (expires_at, license_key),
        )
    elif product_row and is_sound_product(product_row):
        license_key = None
    else:
        # Tạo license key mới cho đơn mua mới
        license_key = f"{order['product_id'][:2].upper()}-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[8:16].upper()}"
        expires_at = _expires_at_from_duration(duration_code)

        # Tạo license record trong bảng licenses (để activate sau này)
        license_secret = secrets.token_urlsafe(32)[:32]
        license_secret_hash = hash_license_secret(license_secret)
        license_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO licenses (id, license_key, license_secret_hash, product_id, user_id, expires_at, status, display_title)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
            ON CONFLICT (license_key) DO UPDATE
            SET license_secret_hash = EXCLUDED.license_secret_hash,
                user_id = EXCLUDED.user_id,
                status = 'active',
                activated_at = NOW(),
                expires_at = EXCLUDED.expires_at,
                display_title = COALESCE(EXCLUDED.display_title, licenses.display_title)
        """, (license_id, license_key, license_secret_hash, order['product_id'], user_id, expires_at, display_title))

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
        'isSoundOrder': bool(product_row and is_sound_product(product_row) and not renew_key),
        'remainingBalance': balance - order_price,
        'order': {
            'id': order_id,
            'price': order_price,
            'status': 'completed',
        }
    })


# ─── POST /api/wallet/reset-hwid ─────────────────────────────────────────────
@wallet_bp.route('/reset-hwid', methods=['POST'])
@require_auth
def reset_hwid_user():
    """
    Xóa gắn máy (machine_id) cho license của user.
    - pay: trừ 10.000đ từ ví, xóa gắn ngay.
    - schedule_free: hẹn đổi miễn phí sau 24h.
    - apply_free: sau khi đủ 24h, xóa gắn không thu phí.
    """
    data = request.get_json() or {}
    license_key = (data.get('licenseKey') or '').strip()
    action = (data.get('action') or '').strip()
    if not license_key:
        return jsonify({'error': 'licenseKey là bắt buộc'}), 400
    if action not in ('pay', 'schedule_free', 'apply_free'):
        return jsonify({'error': 'action không hợp lệ'}), 400

    user_id = request.user['id']
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT license_key, user_id, machine_id, status, free_hwid_reset_after
        FROM licenses WHERE license_key = %s AND user_id = %s
        """,
        (license_key, user_id),
    )
    lic = cur.fetchone()
    if not lic:
        cur.close()
        return jsonify({'error': 'Không tìm thấy license hoặc không thuộc tài khoản này'}), 404
    if (lic.get('status') or 'active') != 'active':
        cur.close()
        return jsonify({'error': 'Key không hoạt động, không thể đổi ID'}), 400

    mid = str(lic.get('machine_id') or '').strip()
    now = datetime.now(timezone.utc)

    if action == 'pay':
        if not mid:
            cur.close()
            return jsonify({'error': 'Key chưa gắn máy, không cần đổi ID'}), 400
        _ensure_wallet(user_id)
        cur.execute('SELECT balance FROM wallets WHERE user_id = %s', (user_id,))
        w = cur.fetchone()
        balance = w['balance'] if w else 0
        if balance < HWID_RESET_PRICE:
            cur.close()
            return jsonify({
                'error': 'Số dư không đủ',
                'required': HWID_RESET_PRICE,
                'current': balance,
            }), 400
        cur.execute(
            'UPDATE wallets SET balance = balance - %s, updated_at = NOW() WHERE user_id = %s',
            (HWID_RESET_PRICE, user_id),
        )
        cur.execute(
            """
            UPDATE licenses SET machine_id = NULL, machine_fingerprint = NULL,
            free_hwid_reset_after = NULL WHERE license_key = %s
            """,
            (license_key,),
        )
        conn.commit()
        cur.close()
        return jsonify({
            'success': True,
            'message': 'Đã xóa gắn máy. Bạn có thể kích hoạt lại trên máy mới.',
            'remainingBalance': balance - HWID_RESET_PRICE,
        })

    if action == 'schedule_free':
        if not mid:
            cur.close()
            return jsonify({'error': 'Key chưa gắn máy, không cần đổi ID'}), 400
        fr = lic.get('free_hwid_reset_after')
        if fr is not None:
            fr_dt = _dt_utc(fr)
            if fr_dt > now:
                cur.close()
                return jsonify({
                    'success': True,
                    'freeHwidResetAfter': fr_dt.isoformat(),
                    'message': 'Bạn đã hẹn đổi ID miễn phí. Chờ đủ thời gian rồi bấm xác nhận.',
                })
            cur.close()
            return jsonify({
                'error': 'Đã đến hạn — hãy bấm xác nhận đổi ID miễn phí.',
                'freeHwidResetAfter': fr_dt.isoformat(),
            }), 400
        ready = now + FREE_HWID_RESET_DELTA
        cur.execute(
            'UPDATE licenses SET free_hwid_reset_after = %s WHERE license_key = %s',
            (ready, license_key),
        )
        conn.commit()
        cur.close()
        return jsonify({
            'success': True,
            'freeHwidResetAfter': ready.isoformat(),
            'message': 'Sau 24 giờ bạn có thể xác nhận đổi ID miễn phí.',
        })

    if action == 'apply_free':
        if not mid:
            cur.close()
            return jsonify({'error': 'Key chưa gắn máy'}), 400
        fr = lic.get('free_hwid_reset_after')
        if fr is None:
            cur.close()
            return jsonify({'error': 'Chưa đăng ký đổi ID miễn phí hoặc đã xử lý'}), 400
        fr_dt = _dt_utc(fr)
        if fr_dt > now:
            cur.close()
            return jsonify({
                'error': 'Chưa đủ 24 giờ',
                'freeHwidResetAfter': fr_dt.isoformat(),
            }), 400
        cur.execute(
            """
            UPDATE licenses SET machine_id = NULL, machine_fingerprint = NULL,
            free_hwid_reset_after = NULL WHERE license_key = %s
            """,
            (license_key,),
        )
        conn.commit()
        cur.close()
        return jsonify({
            'success': True,
            'message': 'Đã xóa gắn máy miễn phí.',
        })

    cur.close()
    return jsonify({'error': 'Lỗi không xác định'}), 500

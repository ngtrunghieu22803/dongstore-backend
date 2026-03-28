"""
License Validation API — dùng PostgreSQL (thay Firebase).

Validate license code + HWID từ bảng `licenses`.
Response ký bằng Ed25519 → Desktop App verify bằng public key.

Env vars cần set:
    FERNET_KEY                — Fernet symmetric key, trả về client khi OK
    SIGNING_PRIVATE_KEY_B64   — base64 của 32-byte Ed25519 private key
    ADMIN_KEY                 — bảo vệ admin endpoints (X-Admin-Key header)
"""

import base64
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.fernet import Fernet
from flask import Blueprint, request, jsonify, current_app

from db import get_db

validate_bp = Blueprint("validate", __name__)


# ── Lazy-loaded signing key ───────────────────────────────────────────────────

def _get_signing_key() -> Optional[Ed25519PrivateKey]:
    b64 = current_app.config.get("SIGNING_PRIVATE_KEY_B64", "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        if len(raw) != 32:
            return None
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception:
        return None


def _get_fernet_key() -> str:
    return current_app.config.get("FERNET_KEY", "").strip()


def _get_fernet() -> Optional[Fernet]:
    fk = _get_fernet_key()
    if not fk:
        return None
    try:
        return Fernet(fk.encode())
    except Exception:
        return None


# ── Sign helpers ─────────────────────────────────────────────────────────────

def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _signed_response(payload: dict):
    key = _get_signing_key()
    if key is None:
        return jsonify({"error": "Server signing key missing"}), 500
    sig = key.sign(_canonical_json_bytes(payload))
    return jsonify({
        "payload": payload,
        "sig": base64.b64encode(sig).decode("ascii"),
    })


def _make_payload(ok: bool, msg: str, code: str, hwid: str, info: Optional[dict] = None) -> dict:
    payload = {
        "v": 2,
        "s": 1 if ok else 0,
        "m": msg,
        "t": int(time.time()),
        "code": code,
        "hwid": hwid,
        "nonce": secrets.token_hex(8),
        "f": _get_fernet_key() if ok else "",
    }
    if isinstance(info, dict):
        if info.get("type"):
            payload["type"] = info["type"]
        if info.get("name"):
            payload["n"] = info["name"]
        # Thêm ngày hết hạn vào payload (client dùng offline)
        if info.get("expires_at"):
            payload["x"] = info["expires_at"]
    return payload


# ── Admin decorator ───────────────────────────────────────────────────────────

def _require_admin_key(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        admin_key = current_app.config.get("ADMIN_KEY", "").strip()
        if not admin_key:
            return jsonify({"error": "ADMIN_KEY not configured"}), 500
        if request.headers.get("X-Admin-Key", "") != admin_key:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC KEY
# ══════════════════════════════════════════════════════════════════════════════

@validate_bp.route("/public-key", methods=["GET"])
def public_key():
    """Trả Ed25519 public key để Desktop App verify signed responses."""
    key = _get_signing_key()
    if key is None:
        return jsonify({"error": "Signing key not configured"}), 500
    
    # Hỗ trợ thư viện cryptography phiên bản cũ trên VPS (Python 3.8)
    try:
        pub = key.public_key().public_bytes_raw()
    except AttributeError:
        from cryptography.hazmat.primitives import serialization
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
    return jsonify({"alg": "Ed25519", "pub": base64.b64encode(pub).decode("ascii")})


# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATE ENDPOINT — PostgreSQL
# ══════════════════════════════════════════════════════════════════════════════

@validate_bp.route("/validate", methods=["POST"])
def validate():
    """
    Validate license code + HWID từ PostgreSQL.

    Request:  { "code": "DS-XXXXXXXX-XXXXXXXX", "hwid": "machine_id" }
    Response: { "payload": {...}, "sig": "base64_ed25519_sig" }

    payload.s = 1 (OK) / 0 (FAIL)
    payload.f = FERNET_KEY khi thành công
    payload.type = "permanent" | "days"
    payload.x = ngày hết hạn (ISO) nếu type=days
    """
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    hwid = str(data.get("hwid", "")).strip()

    if not code or not hwid:
        return _signed_response(_make_payload(False, "Thieu code hoac hwid.", code, hwid))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT l.*, p.name AS product_name
        FROM licenses l
        LEFT JOIN products p ON l.product_id = p.id
        WHERE l.license_key = %s
    """, (code,))
    lic = cur.fetchone()

    if not lic:
        cur.close()
        conn.close()
        return _signed_response(_make_payload(False, "License code khong ton tai.", code, hwid))

    if lic["status"] != "active":
        cur.close()
        conn.close()
        return _signed_response(_make_payload(False, "License da bi vo hieu hoa.", code, hwid))

    # ── Kiểm tra hết hạn ────────────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    expires_at = lic.get("expires_at")
    key_type = "permanent"
    remaining_days = None
    expires_iso = None

    if expires_at:
        key_type = "days"
        # Normalize timezone
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_iso = expires_at.strftime("%Y-%m-%d")
        if now_utc > expires_at:
            cur.close()
            conn.close()
            return _signed_response(
                _make_payload(False, f"License het han ({expires_iso}).", code, hwid)
            )
        remaining_days = max(0, (expires_at - now_utc).days)

    custom_title = (lic.get("display_title") or "").strip()
    info = {
        "type": key_type,
        "name": custom_title or (lic.get("product_name") or ""),
        "expires_at": expires_iso,
    }

    # ── HWID binding ─────────────────────────────────────────────────────────
    bound_hwid = lic.get("machine_id")

    if not bound_hwid:
        # Chưa bind → gán HWID
        cur.execute(
            "UPDATE licenses SET machine_id = %s, last_check_at = NOW() WHERE license_key = %s",
            (hwid, code),
        )
        conn.commit()
        cur.close()
        conn.close()
        msg = f"License hop le. HWID da gan: {hwid}"
        if remaining_days is not None:
            msg += f" Con {remaining_days} ngay."
        return _signed_response(_make_payload(True, msg, code, hwid, info))

    if bound_hwid != hwid:
        cur.close()
        conn.close()
        return _signed_response(
            _make_payload(
                False,
                f"License da kich hoat tren may khac.\nMay nay: {hwid}",
                code, hwid,
            )
        )

    # ── OK: cập nhật last_check_at ───────────────────────────────────────────
    cur.execute(
        "UPDATE licenses SET last_check_at = NOW() WHERE license_key = %s",
        (code,),
    )
    conn.commit()
    cur.close()
    conn.close()

    msg = "License hop le."
    if remaining_days is not None:
        msg = f"License hop le. Con {remaining_days} ngay."
    return _signed_response(_make_payload(True, msg, code, hwid, info))


# ══════════════════════════════════════════════════════════════════════════════
#  UPDATE CHECK — vẫn dùng Firebase (update metadata lưu trên Firebase)
# ══════════════════════════════════════════════════════════════════════════════

@validate_bp.route("/update/latest", methods=["GET"])
def update_latest():
    """Plain GET — trả metadata bản cập nhật mới nhất từ Firebase."""
    try:
        from firebase_keys import fb_get, is_firebase_configured
        if not is_firebase_configured():
            return jsonify({"version": "", "download_url": "", "notes": ""})
        update_info = fb_get("update/latest")
        if not update_info or not isinstance(update_info, dict):
            return jsonify({"version": "", "download_url": "", "notes": ""})
        return jsonify({
            "version": str(update_info.get("version", "")).strip(),
            "download_url": str(update_info.get("download_url", "")).strip(),
            "notes": str(update_info.get("notes", "")).strip(),
        })
    except Exception as e:
        return jsonify({"version": "", "download_url": "", "notes": "", "error": str(e)})


@validate_bp.route("/update/check", methods=["POST"])
def update_check():
    """Encrypted POST — Fernet encrypt/decrypt."""
    fernet = _get_fernet()
    if fernet is None:
        return jsonify({"error": "FERNET_KEY not configured"}), 500

    try:
        enc_body = request.get_data()
        if not enc_body:
            return jsonify({"error": "Empty request body"}), 400
        plain = fernet.decrypt(enc_body)
        json.loads(plain.decode("utf-8"))  # validate JSON
    except Exception as e:
        return jsonify({"error": f"Decrypt error: {e}"}), 400

    try:
        from firebase_keys import fb_get, is_firebase_configured
        update_info = fb_get("update/latest") if is_firebase_configured() else None
    except Exception:
        update_info = None

    response = {
        "version": str((update_info or {}).get("version", "")).strip(),
        "download_url": str((update_info or {}).get("download_url", "")).strip(),
        "notes": str((update_info or {}).get("notes", "")).strip(),
    }

    try:
        enc_resp = fernet.encrypt(json.dumps(response, ensure_ascii=False).encode())
        return enc_resp, 200, {"Content-Type": "application/octet-stream"}
    except Exception as e:
        return jsonify({"error": f"Encrypt error: {e}"}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN KEY API — PostgreSQL
# ══════════════════════════════════════════════════════════════════════════════

@validate_bp.route("/admin/keys", methods=["GET"])
@_require_admin_key
def list_keys():
    """
    List license keys từ PostgreSQL.
    Query params: status, q (search), page, per_page
    """
    status = request.args.get("status", "").strip()
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    offset = (page - 1) * per_page

    where = []
    params = []
    if status:
        where.append("l.status = %s")
        params.append(status)
    if q:
        like = f"%{q}%"
        where.append(
            "(l.license_key ILIKE %s OR COALESCE(u.email,'') ILIKE %s OR COALESCE(p.name,'') ILIKE %s)"
        )
        params.extend([like, like, like])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT l.*, p.name AS product_name, u.email AS user_email
        FROM licenses l
        LEFT JOIN products p ON l.product_id = p.id
        LEFT JOIN users u ON l.user_id = u.id
        {where_sql}
        ORDER BY l.created_at DESC
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])
    rows = cur.fetchall()

    keys = []
    for r in rows:
        r = dict(r)
        r["expires_at"] = r["expires_at"].isoformat() if r.get("expires_at") else None
        r["activated_at"] = r["activated_at"].isoformat() if r.get("activated_at") else None
        r["last_check_at"] = r["last_check_at"].isoformat() if r.get("last_check_at") else None
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
        # Map về format Douyin cho dễ đọc
        r["active"] = r["status"] == "active"
        r["hwid"] = r.get("machine_id")
        r["type"] = "permanent" if not r.get("expires_at") else "days"
        keys.append(r)

    cur.close()
    conn.close()
    return jsonify({"status": "ok", "keys": keys, "count": len(keys)})


@validate_bp.route("/admin/keys", methods=["POST"])
@_require_admin_key
def create_key():
    """
    Tạo license key thủ công (không qua đơn hàng).

    Body:
    {
        "product_id": "tc001",      // bắt buộc
        "type": "permanent"|"days", // mặc định permanent
        "days": 30,                 // nếu type=days
        "user_id": "...",           // optional
        "note": "Tặng khách VIP"    // optional, lưu vào machine_fingerprint tạm
    }
    """
    import uuid as _uuid
    data = request.get_json(silent=True) or {}
    product_id = (data.get("product_id") or "").strip()
    key_type = (data.get("type") or "permanent").strip()
    days = int(data.get("days") or 0)
    user_id = (data.get("user_id") or "").strip() or None
    note = (data.get("note") or "").strip() or None
    display_title = (data.get("display_title") or data.get("displayTitle") or "").strip() or None
    if display_title:
        display_title = display_title[:120]

    if not product_id:
        return jsonify({"status": "error", "message": "product_id là bắt buộc"}), 400

    conn = get_db()
    cur = conn.cursor()

    # Kiểm tra product tồn tại
    cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"status": "error", "message": "Sản phẩm không tồn tại"}), 404

    # Tính expires_at
    now_utc = datetime.now(timezone.utc)
    expires_at = None
    if key_type == "days" and days > 0:
        expires_at = now_utc + timedelta(days=days)

    # Sinh license key & secret
    uid = secrets.token_hex(8).upper()
    license_key = f"DS-{uid[:8]}-{uid[8:16]}"
    license_secret = secrets.token_urlsafe(32)[:32]
    import hashlib
    license_secret_hash = hashlib.sha256(license_secret.encode()).hexdigest()
    license_id = str(_uuid.uuid4())

    try:
        cur.execute("""
            INSERT INTO licenses
                (id, license_key, license_secret_hash, product_id, user_id, expires_at, status, machine_fingerprint, display_title)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s)
        """, (license_id, license_key, license_secret_hash, product_id, user_id, expires_at, note, display_title))
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"status": "error", "message": str(e)}), 500

    cur.close()
    conn.close()

    return jsonify({
        "status": "ok",
        "code": license_key,
        "licenseKey": license_key,
        "licenseSecret": license_secret,   # trả 1 lần duy nhất
        "type": key_type,
        "days": days if key_type == "days" else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }), 201


@validate_bp.route("/admin/keys/<code>", methods=["PATCH"])
@_require_admin_key
def toggle_key(code):
    """Toggle active/inactive key."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status FROM licenses WHERE license_key = %s", (code,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"status": "error", "message": "Key not found."}), 404

    new_status = "inactive" if row["status"] == "active" else "active"
    cur.execute(
        "UPDATE licenses SET status = %s WHERE license_key = %s",
        (new_status, code),
    )
    conn.commit()
    cur.close()
    conn.close()
    lbl = "kich hoat" if new_status == "active" else "huy kich hoat"
    return jsonify({
        "status": "ok",
        "message": f"Key {code} da {lbl}.",
        "active": new_status == "active",
    })


@validate_bp.route("/admin/keys/<code>", methods=["DELETE"])
@_require_admin_key
def delete_key(code):
    """Xóa key khỏi PostgreSQL."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM licenses WHERE license_key = %s", (code,))
    conn.commit()
    deleted = cur.rowcount
    cur.close()
    conn.close()
    if deleted:
        return jsonify({"status": "ok", "message": f"Key {code} da bi xoa."})
    return jsonify({"status": "error", "message": "Key not found."}), 404


@validate_bp.route("/admin/keys/<code>/reset-hwid", methods=["POST"])
@_require_admin_key
def reset_hwid(code):
    """Reset HWID (machine_id) của key."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE licenses SET machine_id = NULL, machine_fingerprint = NULL WHERE license_key = %s",
        (code,),
    )
    conn.commit()
    updated = cur.rowcount
    cur.close()
    conn.close()
    if updated:
        return jsonify({"status": "ok", "message": f"HWID cua {code} da duoc reset."})
    return jsonify({"status": "error", "message": "Key not found."}), 404


@validate_bp.route("/admin/update", methods=["POST"])
@_require_admin_key
def admin_set_update():
    """Set update metadata lên Firebase (update info vẫn lưu Firebase)."""
    data = request.get_json(silent=True) or {}
    version = str(data.get("version", "")).strip()
    download_url = str(data.get("download_url", "")).strip()
    notes = str(data.get("notes", "")).strip()

    if not version or not download_url:
        return jsonify({"status": "error", "message": "Thieu version hoac download_url."}), 400

    try:
        from firebase_keys import fb_put, is_firebase_configured
        if not is_firebase_configured():
            return jsonify({"status": "error", "message": "Firebase chưa cấu hình"}), 500
        update_info = {
            "version": version,
            "download_url": download_url,
            "notes": notes,
            "updated_at": datetime.now().isoformat(),
        }
        ok = fb_put("update/latest", update_info)
        if ok:
            return jsonify({"status": "ok", "info": update_info})
        return jsonify({"status": "error", "message": "Firebase write failed."}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

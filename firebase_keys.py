"""
Firebase Realtime DB helpers — tạo/quản lý license key trên Firebase.
Dùng chung DB với server Douyin (f:\\douyin\\server\\app.py).

Env vars cần set trong .env:
    FIREBASE_URL      — URL Firebase Realtime DB
    FIREBASE_AUTH     — Firebase auth secret
"""

import json
import os
import secrets
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Optional


def _get_firebase_url() -> str:
    return os.environ.get("FIREBASE_URL", "")


def _get_firebase_auth() -> str:
    return os.environ.get("FIREBASE_AUTH", "")


# ── Firebase CRUD ────────────────────────────────────────────────────────────

def fb_get(path: str):
    """GET data từ Firebase Realtime DB."""
    url = _get_firebase_url()
    auth = _get_firebase_auth()
    if not url or not auth:
        return None
    try:
        full_url = f"{url}/{path}.json?auth={auth}&t={int(time.time())}"
        req = urllib.request.Request(full_url, headers={"User-Agent": "DongStoreAPI/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                return None
            return data
    except Exception as e:
        print(f"[FIREBASE] GET {path} failed: {e}")
        return None


def fb_put(path: str, value) -> bool:
    """PUT (overwrite) data vào Firebase."""
    url = _get_firebase_url()
    auth = _get_firebase_auth()
    if not url or not auth:
        return False
    try:
        full_url = f"{url}/{path}.json?auth={auth}"
        body = json.dumps(value).encode("utf-8")
        req = urllib.request.Request(
            full_url,
            data=body,
            method="PUT",
            headers={"User-Agent": "DongStoreAPI/1.0", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[FIREBASE] PUT {path} failed: {e}")
        return False


def fb_patch(path: str, value) -> bool:
    """PATCH (merge) data vào Firebase."""
    url = _get_firebase_url()
    auth = _get_firebase_auth()
    if not url or not auth:
        return False
    try:
        full_url = f"{url}/{path}.json?auth={auth}"
        body = json.dumps(value).encode("utf-8")
        req = urllib.request.Request(
            full_url,
            data=body,
            method="PATCH",
            headers={"User-Agent": "DongStoreAPI/1.0", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[FIREBASE] PATCH {path} failed: {e}")
        return False


def fb_delete(path: str) -> bool:
    """DELETE data trên Firebase."""
    url = _get_firebase_url()
    auth = _get_firebase_auth()
    if not url or not auth:
        return False
    try:
        full_url = f"{url}/{path}.json?auth={auth}"
        req = urllib.request.Request(full_url, method="DELETE", headers={"User-Agent": "DongStoreAPI/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[FIREBASE] DELETE {path} failed: {e}")
        return False


# ── License Key Operations ───────────────────────────────────────────────────

def generate_firebase_key_code() -> str:
    """Tạo key code format giống server Douyin: XXXX-XXXX-XXXX."""
    parts = [secrets.token_hex(2).upper() for _ in range(3)]
    return "-".join(parts)


def create_firebase_key(
    key_type: str = "permanent",
    days: int = 0,
    name: str = "",
    code: Optional[str] = None,
) -> Optional[str]:
    """
    Tạo license key trên Firebase (cùng format với server Douyin).

    Args:
        key_type: "permanent" hoặc "days"
        days: Số ngày (chỉ dùng khi key_type="days")
        name: Tên khách hàng / ghi chú
        code: Key code cụ thể (nếu None thì tự generate)

    Returns:
        Key code nếu thành công, None nếu thất bại.
    """
    if code is None:
        code = generate_firebase_key_code()

    today = datetime.now().strftime("%Y-%m-%d")

    entry = {
        "active": True,
        "type": key_type,
        "hwid": None,
        "created": today,
        "source": "dongstore",  # đánh dấu key tạo từ backend bán hàng
    }
    if name:
        entry["name"] = name
    if key_type == "days" and days > 0:
        entry["days"] = days

    ok = fb_put(f"keys/{code}", entry)
    if ok:
        print(f"[FIREBASE] ✅ Created key: {code} (type={key_type}, days={days})")
        return code
    else:
        print(f"[FIREBASE] ❌ Failed to create key: {code}")
        return None


def deactivate_firebase_key(code: str) -> bool:
    """Vô hiệu hóa key trên Firebase."""
    return fb_patch(f"keys/{code}", {"active": False})


def activate_firebase_key(code: str) -> bool:
    """Kích hoạt lại key trên Firebase."""
    return fb_patch(f"keys/{code}", {"active": True})


def reset_firebase_hwid(code: str) -> bool:
    """Reset HWID của key trên Firebase."""
    return fb_patch(f"keys/{code}", {"hwid": None})


def delete_firebase_key(code: str) -> bool:
    """Xóa key khỏi Firebase."""
    return fb_delete(f"keys/{code}")


def get_firebase_key(code: str) -> Optional[dict]:
    """Lấy thông tin key từ Firebase."""
    return fb_get(f"keys/{code}")


def list_firebase_keys() -> Optional[dict]:
    """Lấy tất cả keys từ Firebase."""
    return fb_get("keys")


def is_firebase_configured() -> bool:
    """Kiểm tra Firebase đã được cấu hình chưa."""
    return bool(_get_firebase_url()) and bool(_get_firebase_auth())

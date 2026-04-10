"""Giới hạn tần suất theo IP hoặc theo user (in-memory), dùng chung cho các API nhạy cảm."""
import time
from threading import Lock
from typing import Optional

from flask import jsonify, make_response, request

_RL: dict = {}
_RL_LOCK = Lock()

RATE_LIMIT_MSG = "Quá nhiều yêu cầu. Vui lòng thử lại sau."


def _client_ip() -> str:
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def hit_rate(bucket: str, limit: int, window_sec: int, user_id: Optional[str] = None) -> bool:
    """
    Trả về True nếu cho phép tiếp tục.
    - user_id: khi có (API đã auth), giới hạn theo user — công bằng hơn với NAT.
    - Không có user_id: giới hạn theo IP.
    """
    if user_id:
        key = f"{bucket}:u:{user_id}"
    else:
        key = f"{bucket}:ip:{_client_ip()}"
    now = int(time.time())
    with _RL_LOCK:
        entries = [t for t in _RL.get(key, []) if t > now - window_sec]
        if len(entries) >= limit:
            _RL[key] = entries
            return False
        entries.append(now)
        _RL[key] = entries
        return True


def response_429(window_sec: int):
    """JSON 429 + Retry-After (giây)."""
    r = make_response(jsonify({"error": RATE_LIMIT_MSG}), 429)
    r.headers["Retry-After"] = str(window_sec)
    return r

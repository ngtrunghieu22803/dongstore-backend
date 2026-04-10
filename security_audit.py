"""
Ghi nhật ký bảo mật (best-effort — không làm hỏng luồng request nếu DB lỗi).
"""
import json
import uuid
from typing import Optional

from flask import has_request_context, request

from db import get_db


def _client_ip() -> Optional[str]:
    if not has_request_context():
        return None
    raw = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    return raw[:128] if raw else None


def _user_agent() -> Optional[str]:
    if not has_request_context():
        return None
    return (request.headers.get("User-Agent") or "")[:512] or None


def log_security_event(
    event_type: str,
    message: Optional[str] = None,
    extra: Optional[dict] = None,
    user_id: Optional[str] = None,
) -> None:
    """Ghi một dòng vào security_audit_log."""
    try:
        rid = str(uuid.uuid4())
        extra_s = json.dumps(extra, ensure_ascii=False) if extra else None
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO security_audit_log (id, event_type, message, ip, user_agent, user_id, extra)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                rid,
                (event_type or "unknown")[:100],
                (message or "")[:4000] if message else None,
                _client_ip(),
                _user_agent(),
                user_id,
                extra_s,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

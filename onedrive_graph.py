"""
Upload/stream/xóa file âm thanh trên OneDrive for Business qua Microsoft Graph (client credentials).

Cần đăng ký app Azure AD, cấp quyền Application: Files.ReadWrite.All (admin consent).
Biến môi trường:
  MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET
  MS_GRAPH_DRIVE_USER — UPN (email) hoặc object id của user sở hữu OneDrive
  ONEDRIVE_SOUNDS_FOLDER — tiền tố trong OneDrive (mặc định DongStore); file = folder/object_name (vd. DongStore/sounds/abc.mp3)
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Dict, Optional, Tuple

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"


def _cfg_from_app() -> Dict[str, str]:
    try:
        from flask import current_app

        return {
            "tenant_id": (current_app.config.get("MS_GRAPH_TENANT_ID") or "").strip(),
            "client_id": (current_app.config.get("MS_GRAPH_CLIENT_ID") or "").strip(),
            "client_secret": (current_app.config.get("MS_GRAPH_CLIENT_SECRET") or "").strip(),
            "drive_user": (current_app.config.get("MS_GRAPH_DRIVE_USER") or "").strip(),
            "folder": (
                (current_app.config.get("ONEDRIVE_SOUNDS_FOLDER") or "DongStore")
                .strip()
                .strip("/")
            ),
        }
    except RuntimeError:
        return _cfg_from_env()


def _cfg_from_env() -> Dict[str, str]:
    return {
        "tenant_id": os.environ.get("MS_GRAPH_TENANT_ID", "").strip(),
        "client_id": os.environ.get("MS_GRAPH_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("MS_GRAPH_CLIENT_SECRET", "").strip(),
        "drive_user": os.environ.get("MS_GRAPH_DRIVE_USER", "").strip(),
        "folder": os.environ.get("ONEDRIVE_SOUNDS_FOLDER", "DongStore").strip().strip("/"),
    }


def onedrive_configured() -> bool:
    c = _cfg_from_app()
    return bool(
        c["tenant_id"] and c["client_id"] and c["client_secret"] and c["drive_user"]
    )


def _encode_drive_path(relative: str) -> str:
    return "/".join(urllib.parse.quote(seg, safe="") for seg in relative.split("/") if seg)


def _user_segment(user: str) -> str:
    return urllib.parse.quote(user, safe="")


def _acquire_token(cfg: Dict[str, str]) -> str:
    app = msal.ConfidentialClientApplication(
        cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
        client_credential=cfg["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or "token failed"
        raise RuntimeError(err)
    return result["access_token"]


def _headers(cfg: Dict[str, str]) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_acquire_token(cfg)}",
    }


# Graph giới hạn PUT /content ~4MB; file lớn hơn dùng upload session.
_SIMPLE_PUT_MAX = 4 * 1024 * 1024
_CHUNK = 10 * 320 * 1024  # bội số 320 KiB (khuyến nghị OneDrive)


def _graph_upload_session(
    cfg: Dict[str, str], enc: str, user: str, data: bytes
) -> Tuple[int, Optional[str]]:
    session_url = f"{GRAPH}/users/{user}/drive/root:/{enc}:/createUploadSession"
    r = requests.post(
        session_url,
        headers={**_headers(cfg), "Content-Type": "application/json"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        timeout=120,
    )
    if r.status_code != 200:
        return r.status_code, r.text
    upload_url = r.json().get("uploadUrl")
    if not upload_url:
        return 500, r.text

    total = len(data)
    start = 0
    while start < total:
        end = min(start + _CHUNK, total)
        chunk = data[start:end]
        clen = len(chunk)
        headers = {
            "Content-Length": str(clen),
            "Content-Range": f"bytes {start}-{start + clen - 1}/{total}",
        }
        # Không gửi Authorization lên uploadUrl (URL đã ủy quyền sẵn).
        resp = requests.put(upload_url, headers=headers, data=chunk, timeout=300)
        if resp.status_code not in (200, 201, 202):
            return resp.status_code, (resp.text or "")[:500]
        if resp.status_code in (200, 201):
            return 200, None
        start = end
    return 200, None


def _get_or_create_folder(cfg: Dict[str, str], parent_path: str, folder_name: str) -> None:
    """Tạo folder con 'folder_name' bên trong 'parent_path' trên OneDrive. Bỏ qua nếu đã có."""
    user = _user_segment(cfg["drive_user"])
    if parent_path:
        parent_path_enc = _encode_drive_path(parent_path)
        check_url = f"{GRAPH}/users/{user}/drive/root:/{parent_path_enc}/{urllib.parse.quote(folder_name, safe='')}"
    else:
        check_url = f"{GRAPH}/users/{user}/drive/root/children/{urllib.parse.quote(folder_name, safe='')}"
    r = requests.get(check_url, headers=_headers(cfg), timeout=30)
    if r.status_code == 200:
        return  # folder đã tồn tại

    # Tạo folder mới
    if parent_path:
        create_url = f"{GRAPH}/users/{user}/drive/root:/{parent_path_enc}:/children"
    else:
        create_url = f"{GRAPH}/users/{user}/drive/root/children"
    r = requests.post(
        create_url,
        headers={**_headers(cfg), "Content-Type": "application/json"},
        json={
            "name": folder_name,
            "folder": {},
        },
        timeout=30,
    )
    if r.status_code not in (200, 201, 409):
        raise RuntimeError(f"Không tạo được folder '{folder_name}' trong '{parent_path}': {r.status_code} {r.text[:200]}")


def _ensure_folder_structure(cfg: Dict[str, str], relative_path: str) -> None:
    """Đảm bảo tất cả folder trong đường dẫn đều tồn tại."""
    parts = [p for p in relative_path.split("/") if p]
    if len(parts) <= 1:
        return
    parent = ""
    for part in parts[:-1]:
        _get_or_create_folder(cfg, parent, part)
        parent = f"{parent}/{part}" if parent else part


def graph_put_file(
    relative_path: str, data: bytes, content_type: str
) -> Tuple[int, Optional[str]]:
    """Upload nội dung file (Graph): PUT đơn giản hoặc upload session nếu > ~4MB."""
    cfg = _cfg_from_app()
    enc = _encode_drive_path(relative_path)
    user = _user_segment(cfg["drive_user"])
    _ensure_folder_structure(cfg, enc)
    if len(data) > _SIMPLE_PUT_MAX:
        return _graph_upload_session(cfg, enc, user, data)

    url = f"{GRAPH}/users/{user}/drive/root:/{enc}:/content"
    r = requests.put(
        url,
        headers={**_headers(cfg), "Content-Type": content_type},
        data=data,
        timeout=120,
    )
    return r.status_code, r.text if r.status_code >= 400 else None


def graph_get_file(relative_path: str) -> Tuple[bytes, str]:
    cfg = _cfg_from_app()
    enc = _encode_drive_path(relative_path)
    user = _user_segment(cfg["drive_user"])
    url = f"{GRAPH}/users/{user}/drive/root:/{enc}:/content"
    r = requests.get(url, headers=_headers(cfg), timeout=120)
    if r.status_code != 200:
        raise ValueError(f"Graph GET content {r.status_code}: {r.text[:200]}")
    ct = r.headers.get("Content-Type", "application/octet-stream")
    return r.content, ct


def graph_delete_file(relative_path: str) -> None:
    cfg = _cfg_from_app()
    enc = _encode_drive_path(relative_path)
    user = _user_segment(cfg["drive_user"])
    url = f"{GRAPH}/users/{user}/drive/root:/{enc}:"
    r = requests.delete(url, headers=_headers(cfg), timeout=60)
    if r.status_code not in (204, 404):
        raise RuntimeError(f"Graph DELETE {r.status_code}: {r.text[:200]}")


def _rel_for_object(object_name: str) -> str:
    cfg = _cfg_from_app()
    base = cfg["folder"]
    return f"{base}/{object_name}" if base else object_name


def upload_sound_object(object_name: str, data: bytes, content_type: str) -> None:
    """object_name giống MinIO: sounds/{sound_id}.ext"""
    rel = _rel_for_object(object_name)
    code, err = graph_put_file(rel, data, content_type)
    if code not in (200, 201):
        raise RuntimeError(err or f"Graph upload {code}")


def download_sound_by_object_name(object_name: str) -> Tuple[bytes, str]:
    return graph_get_file(_rel_for_object(object_name))


def delete_sound_by_object_name(object_name: str) -> None:
    graph_delete_file(_rel_for_object(object_name))

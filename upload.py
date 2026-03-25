"""
Upload ảnh lên MinIO với tự động convert sang WebP.
"""
import io
import uuid
import requests
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse
from minio import Minio
from PIL import Image
from flask import current_app

import onedrive_graph


def get_minio_client():
    """Khởi tạo MinIO client từ config."""
    return Minio(
        current_app.config.get('MINIO_ENDPOINT', 'minio01.vpsmmo.vn:9000'),
        access_key=current_app.config.get('MINIO_ACCESS_KEY', 'nthieu228'),
        secret_key=current_app.config.get('MINIO_SECRET_KEY', ''),
        secure=True,
    )


def get_bucket_name():
    return current_app.config.get('MINIO_BUCKET', 'bucket-nthieu228')


def get_image_folder():
    return current_app.config.get('MINIO_IMAGE_FOLDER', 'hinh_anh_dong-store')


def ensure_bucket_public():
    """
    Set bucket thành public read-only để ảnh load trực tiếp từ trình duyệt.
    Chỉ cần gọi 1 lần lúc khởi động.
    """
    client = get_minio_client()
    bucket = get_bucket_name()

    # Bucket policy cho phép đọc công khai tất cả objects
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadGetObject",
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket}/*"
            }
        ]
    }

    import json
    policy_str = json.dumps(policy)

    try:
        client.set_bucket_policy(bucket, policy_str)
        print(f"✅ Bucket '{bucket}' đã được set public thành công")
    except Exception as e:
        print(f"⚠️  Không set được bucket policy: {e}")


def upload_image(file, folder=None, max_size=(1200, 1200)):
    """
    Upload ảnh lên MinIO, tự động convert sang WebP.
    
    Args:
        file: File object từ Flask request
        folder: Thư mục trong bucket (products, avatars, etc)
        max_size: Kích thước tối đa (width, height)
    
    Returns:
        dict: { url: 'https://...', filename: 'xxx.webp' }
    """
    if not file:
        raise ValueError('Không có file được upload')
    
    # Đọc ảnh
    img = Image.open(file)
    
    # Convert sang RGB nếu cần (WebP không hỗ trợ RGBA)
    if img.mode in ('RGBA', 'LA', 'P'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = background
    
    # Resize nếu quá lớn (giữ tỷ lệ)
    img.thumbnail(max_size, Image.Resampling.LANCZOS)

    # Dùng folder từ config nếu không truyền vào
    if folder is None:
        folder = get_image_folder()

    # Chuyển sang WebP
    output = io.BytesIO()
    img.save(output, format='WEBP', quality=85, optimize=True)
    output.seek(0)
    
    # Tạo tên file ngẫu nhiên
    ext = '.webp'
    filename = f"{uuid.uuid4().hex}{ext}"
    object_name = f"{folder}/{filename}"
    
    # Upload lên MinIO
    client = get_minio_client()
    bucket = get_bucket_name()
    
    # Tạo bucket nếu chưa có
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    
    # Upload
    client.put_object(
        bucket,
        object_name,
        output,
        length=output.getbuffer().nbytes,
        content_type='image/webp',
    )
    
    # Tạo URL (public read)
    url = f"https://{current_app.config.get('MINIO_ENDPOINT', 'minio01.vpsmmo.vn:9000')}/{bucket}/{object_name}"
    
    return {
        'url': url,
        'filename': filename,
    }


def delete_image(object_name):
    """Xóa ảnh khỏi MinIO."""
    if not object_name:
        return
    
    try:
        client = get_minio_client()
        bucket = get_bucket_name()
        client.remove_object(bucket, object_name)
    except Exception as e:
        print(f"Warning: Failed to delete image {object_name}: {e}")


def get_presigned_url(object_name, expires_hours=24):
    """Lấy URL tạm thời cho object trong bucket mặc định."""
    client = get_minio_client()
    bucket = get_bucket_name()
    return client.presigned_get_object(bucket, object_name, expires=timedelta(hours=expires_hours))


PREVIEW_BUCKET = 'kho-am-thanh-preview'
PREVIEW_FOLDER = 'preview'


def upload_preview_audio(file, product_id: str):
    """
    Upload file audio preview lên MinIO (folder private).
    Args:
        file: File object từ Flask request
        product_id: ID sản phẩm để đặt tên file
    Returns:
        dict: { url: '/api/products/{id}/preview', filename: 'xxx.mp3' }
    Raises:
        ValueError: nếu file không hợp lệ
    """
    import os
    from werkzeug.datastructures import FileStorage

    if not file:
        raise ValueError('Không có file được upload')

    # Validate extension
    allowed = {'mp3', 'wav', 'ogg', 'm4a', 'flac'}
    ext = os.path.splitext(file.filename)[1].lower().lstrip('.')
    if ext not in allowed:
        raise ValueError(f'Định dạng không hỗ trợ: .{ext}. Chỉ chấp nhận: {", ".join(allowed)}')

    # Validate size (max 10MB)
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 10 * 1024 * 1024:
        raise ValueError('File quá lớn. Tối đa 10MB.')

    # Tên file: {product_id}.{ext}
    filename = f"{product_id}.{ext}"
    object_name = f"{PREVIEW_FOLDER}/{filename}"

    client = get_minio_client()

    # Tạo bucket nếu chưa có
    if not client.bucket_exists(PREVIEW_BUCKET):
        client.make_bucket(PREVIEW_BUCKET)

    # Set bucket private (chỉ server đọc được)
    try:
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PrivateNoPublicAccess",
                    "Effect": "Deny",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": f"arn:aws:s3:::{PREVIEW_BUCKET}/*"
                }
            ]
        }
        import json
        client.set_bucket_policy(PREVIEW_BUCKET, json.dumps(policy))
    except Exception:
        pass  # Policy có thể đã có

    # Upload file
    file.seek(0, 2)
    length = file.tell()
    file.seek(0)

    content_types = {
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'ogg': 'audio/ogg',
        'm4a': 'audio/mp4',
        'flac': 'audio/flac',
    }

    client.put_object(
        PREVIEW_BUCKET,
        object_name,
        file,
        length=length,
        content_type=content_types.get(ext, 'audio/mpeg'),
    )

    return {
        'url': f'/api/products/{product_id}/preview',
        'filename': filename,
        'bucket': PREVIEW_BUCKET,
        'object_name': object_name,
    }


def get_preview_object_name(product_id: str, ext: str) -> str:
    return f"{PREVIEW_FOLDER}/{product_id}.{ext}"


def stream_preview_audio(product_id: str):
    """
    Đọc file preview từ MinIO, trả về (file_obj, content_type, filename).
    Raises ValueError nếu không tìm thấy.
    """
    client = get_minio_client()

    # Thử các extension phổ biến
    for ext in ['mp3', 'wav', 'ogg', 'm4a', 'flac']:
        object_name = get_preview_object_name(product_id, ext)
        try:
            response = client.get_object(PREVIEW_BUCKET, object_name)
            content_types = {
                'mp3': 'audio/mpeg',
                'wav': 'audio/wav',
                'ogg': 'audio/ogg',
                'm4a': 'audio/mp4',
                'flac': 'audio/flac',
            }
            return response, content_types[ext], f"{product_id}.{ext}"
        except Exception:
            continue

    raise ValueError('Preview audio not found')


def delete_preview_audio(product_id: str):
    """Xóa preview audio của sản phẩm."""
    client = get_minio_client()
    for ext in ['mp3', 'wav', 'ogg', 'm4a', 'flac']:
        object_name = get_preview_object_name(product_id, ext)
        try:
            client.remove_object(PREVIEW_BUCKET, object_name)
        except Exception:
            pass


def presign_from_public_url(url: str, expires_hours: int = 24) -> str:
    """
    URL dạng path-style: https://host:9000/bucket-name/folder/file.webp
    → presigned GET (bucket private vẫn xem được trong <img>).
    """
    if not url or not str(url).strip():
        raise ValueError('URL rỗng')
    p = urlparse(url.strip())
    parts = [x for x in p.path.split('/') if x]
    if len(parts) < 2:
        raise ValueError('Không parse được object từ URL')
    bucket = parts[0]
    object_name = '/'.join(parts[1:])
    client = get_minio_client()
    return client.presigned_get_object(bucket, object_name, expires=timedelta(hours=expires_hours))


# ─── Standalone Sounds ──────────────────────────────────────────────────────
SOUNDS_BUCKET = 'kho-am-thanh-preview'
SOUNDS_FOLDER = 'sounds'


def upload_sound(file, sound_id: str):
    """
    Upload file audio lên MinIO hoặc OneDrive (nếu cấu hình Graph đủ).
    Args:
        file: File object từ Flask request
        sound_id: ID của sound record
    Returns:
        dict: { object_name, filename, url }
    Raises:
        ValueError: nếu file không hợp lệ
    """
    import os

    if not file:
        raise ValueError('Không có file được upload')

    allowed = {'mp3', 'wav', 'ogg', 'm4a', 'flac'}
    ext = os.path.splitext(file.filename)[1].lower().lstrip('.')
    if ext not in allowed:
        raise ValueError(f'Định dạng không hỗ trợ: .{ext}. Chỉ chấp nhận: {", ".join(allowed)}')

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 10 * 1024 * 1024:
        raise ValueError('File quá lớn. Tối đa 10MB.')

    filename = f"{sound_id}.{ext}"
    object_name = f"{SOUNDS_FOLDER}/{filename}"

    content_types = {
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'ogg': 'audio/ogg',
        'm4a': 'audio/mp4',
        'flac': 'audio/flac',
    }
    content_type = content_types.get(ext, 'audio/mpeg')

    if onedrive_graph.onedrive_configured():
        file.seek(0)
        data = file.read()
        onedrive_graph.upload_sound_object(object_name, data, content_type)
        return {
            'object_name': object_name,
            'filename': filename,
            'ext': ext,
            'size': size,
            'storage': 'onedrive',
        }

    client = get_minio_client()
    if not client.bucket_exists(SOUNDS_BUCKET):
        client.make_bucket(SOUNDS_BUCKET)

    client.put_object(
        SOUNDS_BUCKET,
        object_name,
        file,
        length=size,
        content_type=content_type,
    )

    return {
        'object_name': object_name,
        'filename': filename,
        'ext': ext,
        'size': size,
        'storage': 'minio',
    }


def stream_sound(object_name: str, storage: str = 'minio'):
    """
    Đọc file sound từ MinIO hoặc OneDrive, trả về (stream có .read(), content_type).
    Raises ValueError nếu không tìm thấy.
    """
    if (storage or 'minio') == 'onedrive':
        data, ct = onedrive_graph.download_sound_by_object_name(object_name)
        return io.BytesIO(data), ct

    client = get_minio_client()
    ext = object_name.rsplit('.', 1)[-1].lower()
    content_types = {
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'ogg': 'audio/ogg',
        'm4a': 'audio/mp4',
        'flac': 'audio/flac',
    }
    try:
        response = client.get_object(SOUNDS_BUCKET, object_name)
        return response, content_types.get(ext, 'audio/mpeg')
    except Exception:
        raise ValueError('Sound not found')


def get_sound_direct_url(object_name: str, storage: str = 'minio') -> Optional[str]:
    """
    Trả về URL trực tiếp để frontend chơi audio không qua server.
    - MinIO  → presigned URL (24h)
    - OneDrive → Microsoft Graph downloadUrl
    Trả về None nếu không lấy được.
    """
    if (storage or 'minio') == 'onedrive':
        return _onedrive_download_url(object_name)

    try:
        client = get_minio_client()
        return client.presigned_get_object(
            SOUNDS_BUCKET, object_name, expires=timedelta(hours=24)
        )
    except Exception:
        return None


def _onedrive_download_url(object_name: str) -> Optional[str]:
    """Lấy URL trực tiếp từ OneDrive qua Microsoft Graph."""
    try:
        cfg = onedrive_graph._cfg_from_app()
        if not cfg['tenant_id']:
            return None

        rel = onedrive_graph._rel_for_object(object_name)
        enc = onedrive_graph._encode_drive_path(rel)
        user = onedrive_graph._user_segment(cfg['drive_user'])
        token = onedrive_graph._acquire_token(cfg)
        if not token:
            return None

        url = f"https://graph.microsoft.com/v1.0/users/{user}/drive/root:/{enc}"
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {token}'},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get('@microsoft.graph.downloadUrl')
    except Exception:
        pass
    return None


def delete_sound(object_name: str, storage: str = 'minio'):
    """Xóa sound khỏi MinIO hoặc OneDrive."""
    if (storage or 'minio') == 'onedrive':
        try:
            onedrive_graph.delete_sound_by_object_name(object_name)
        except Exception:
            pass
        return

    client = get_minio_client()
    try:
        client.remove_object(SOUNDS_BUCKET, object_name)
    except Exception:
        pass

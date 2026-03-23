"""
Upload ảnh lên MinIO với tự động convert sang WebP.
"""
import io
import uuid
from datetime import timedelta
from urllib.parse import urlparse
from minio import Minio
from PIL import Image
from flask import current_app


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

"""
Mã hóa/giải mã AES-256-CBC cho API endpoints bảo mật.
"""
import os
import base64
import hashlib
import secrets
import json
import time
from typing import Tuple, Optional, Dict, Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding


# ==============================================================================
# 1. Key Derivation
# ==============================================================================

def derive_key_from_secret(secret: str, salt: str = "") -> bytes:
    """Derive 32-byte key từ secret bằng SHA-256."""
    data = f"{secret}:{salt}".encode('utf-8')
    return hashlib.sha256(data).digest()


def derive_key_from_password(password: str, salt: bytes) -> bytes:
    """Derive key từ password + salt (PBKDF2-lite)."""
    # Sử dụng SHA-256 với salt như một "lightweight KDF"
    combined = password.encode('utf-8') + salt
    return hashlib.sha256(combined).digest()


# ==============================================================================
# 2. AES-256-CBC Encryption/Decryption
# ==============================================================================

def generate_iv() -> bytes:
    """Generate random 16-byte IV."""
    return secrets.token_bytes(16)


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS7 padding."""
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def pkcs7_unpad(data: bytes) -> bytes:
    """Remove PKCS7 padding."""
    if not data:
        return data
    pad_len = data[-1]
    if pad_len > 16 or pad_len == 0:
        return data
    return data[:-pad_len]


def encrypt_aes_256_cbc(plaintext: str, key: bytes) -> Tuple[str, str]:
    """
    Mã hóa plaintext với AES-256-CBC.
    Returns: (ciphertext_base64, iv_base64)
    """
    if isinstance(plaintext, str):
        plaintext = plaintext.encode('utf-8')

    iv = generate_iv()
    padded_data = pkcs7_pad(plaintext)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()

    return (
        base64.b64encode(ciphertext).decode('ascii'),
        base64.b64encode(iv).decode('ascii')
    )


def decrypt_aes_256_cbc(ciphertext_b64: str, key: bytes, iv_b64: Optional[str] = None) -> str:
    """
    Giải mã AES-256-CBC.
    Nếu iv_b64 là None, ciphertext_b64 được giả định chứa iv ở đầu (combined format).
    """
    ciphertext = base64.b64decode(ciphertext_b64)

    if iv_b64 is None:
        # Combined format: iv (16 bytes) + ciphertext
        if len(ciphertext) < 16:
            raise ValueError("Ciphertext too short")
        iv = ciphertext[:16]
        ciphertext = ciphertext[16:]
    else:
        iv = base64.b64decode(iv_b64)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    plaintext = pkcs7_unpad(padded_plaintext)

    return plaintext.decode('utf-8')


# ==============================================================================
# 3. License Secret Generation & Management
# ==============================================================================

def generate_license_secret() -> str:
    """Generate random 32-character license secret."""
    return secrets.token_urlsafe(32)[:32]


def generate_nonce() -> str:
    """Generate random nonce cho request signing."""
    return secrets.token_urlsafe(16)


def generate_timestamp() -> int:
    """Get current timestamp (seconds)."""
    return int(time.time())


def validate_timestamp(ts: int, max_age_seconds: int = 300) -> bool:
    """
    Kiểm tra timestamp có trong khoảng cho phép không.
    Mặc định: timestamp phải trong vòng 5 phút.
    """
    now = generate_timestamp()
    return abs(now - ts) <= max_age_seconds


# ==============================================================================
# 4. Request/Response Encryption Wrapper
# ==============================================================================

class SecureEnvelope:
    """
    Wrapper cho request/response đã mã hóa.

    Format request gửi lên:
    {
        "data": "base64_encrypted_payload",
        "ts": timestamp,
        "nonce": "random_nonce"
    }

    Format response trả về:
    {
        "ok": true/false,
        "data": "base64_encrypted_payload" // nếu ok=true
        "error": "error_message" // nếu ok=false
    }
    """

    @staticmethod
    def wrap_request(payload: Dict[str, Any], key: bytes) -> Dict[str, Any]:
        """Mã hóa payload và tạo request envelope."""
        plaintext = json.dumps(payload, ensure_ascii=False)
        ciphertext, iv = encrypt_aes_256_cbc(plaintext, key)
        return {
            "data": ciphertext,
            "iv": iv,
            "ts": generate_timestamp(),
            "nonce": generate_nonce()
        }

    @staticmethod
    def unwrap_response(response: Dict[str, Any], key: bytes) -> Dict[str, Any]:
        """Giải mã response envelope."""
        if not response.get("ok", False):
            raise ValueError(response.get("error", "Unknown error"))

        ciphertext = response.get("data", "")
        iv = response.get("iv", "")

        plaintext = decrypt_aes_256_cbc(ciphertext, key, iv)
        return json.loads(plaintext)

    @staticmethod
    def create_response(data: Any, key: bytes) -> Dict[str, Any]:
        """Tạo response envelope với data đã mã hóa."""
        if isinstance(data, dict) and "error" in data:
            # Error response
            return {
                "ok": False,
                "error": data["error"]
            }

        plaintext = json.dumps(data, ensure_ascii=False, default=str)
        ciphertext, iv = encrypt_aes_256_cbc(plaintext, key)
        return {
            "ok": True,
            "data": ciphertext,
            "iv": iv
        }

    @staticmethod
    def unwrap_request(request: Dict[str, Any], key: bytes, max_age: int = 300) -> Dict[str, Any]:
        """Giải mã request envelope với timestamp validation."""
        # Validate timestamp
        ts = request.get("ts", 0)
        if not validate_timestamp(ts, max_age):
            raise ValueError("Request expired or timestamp invalid")

        ciphertext = request.get("data", "")
        iv = request.get("iv", "")

        plaintext = decrypt_aes_256_cbc(ciphertext, key, iv)
        return json.loads(plaintext)


# ==============================================================================
# 5. License-Specific Utilities
# ==============================================================================

def build_license_key_with_secret(product_prefix: str = "DS") -> Tuple[str, str]:
    """
    Tạo cặp license_key và license_secret.
    license_key: DS-XXXXXXXX-XXXXXXXX
    license_secret: 32-character random string
    """
    uid = uuid_hex = secrets.token_hex(8).upper()
    license_key = f"{product_prefix}-{uid[:8]}-{uid[8:16]}"
    license_secret = generate_license_secret()
    return license_key, license_secret


def hash_license_secret(secret: str) -> str:
    """Hash license secret để lưu vào DB (giống bcrypt nhưng dùng SHA-256)."""
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def verify_license_secret(secret: str, hashed: str) -> bool:
    """Verify license secret against hash."""
    return hash_license_secret(secret) == hashed


# ==============================================================================
# 6. Machine ID Generation (cho device binding)
# ==============================================================================

def generate_machine_fingerprint(machine_data: str) -> str:
    """
    Tạo machine fingerprint từ dữ liệu máy.
    Sử dụng SHA-256 hash để tạo deterministic ID.
    """
    return hashlib.sha256(machine_data.encode('utf-8')).hexdigest()[:32]


def validate_machine_fingerprint(stored: str, provided: str, tolerance: int = 0) -> bool:
    """
    Validate machine fingerprint.
    tolerance: số ký tự được phép khác nhau (để handle minor hardware changes).
    """
    if abs(len(stored) - len(provided)) > tolerance:
        return False
    matches = sum(c1 == c2 for c1, c2 in zip(stored, provided))
    return matches >= (len(stored) - tolerance)

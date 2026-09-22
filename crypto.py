"""AES-256-GCM envelope for Verdent LLM proxy (reverse-engineered from app.asar).

PROXY_SIGN -> base64 -> first 32 bytes = AES key.
plaintext = JSON(obj); blob = base64(nonce12 | ciphertext | gcm_tag10)
"""
import base64, json, os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SIGN = "codeck502deck_25_09_15v7"
KEY = base64.b64encode(SIGN.encode())[:32]

def encrypt_obj(obj) -> str:
    nonce = os.urandom(12)
    blob = AESGCM(KEY).encrypt(nonce, json.dumps(obj).encode("utf-8"), None)
    return base64.b64encode(nonce + blob).decode("ascii")

def decrypt_blob(b64: str):
    raw = base64.b64decode(b64)
    return json.loads(AESGCM(KEY).decrypt(raw[:12], raw[12:], None))

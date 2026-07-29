"""
DingTalk callback cryptography: signature verify + AES encrypt/decrypt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import struct
import time
from typing import Any

from Crypto.Cipher import AES

BLOCK_SIZE = 16


class DingTalkCallbackCrypto:
    def __init__(self, token: str, aes_key: str, app_key: str):
        self.token = token
        self.app_key = app_key
        self.aes_key = base64.b64decode(aes_key + "=")

    def _sha1_sign(self, *parts: str) -> str:
        raw = "".join(sorted(parts))
        return hashlib.sha1(raw.encode()).hexdigest()

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        computed = self._sha1_sign(self.token, timestamp, nonce, encrypt)
        return msg_signature == computed

    def decrypt(self, encrypt_text: str) -> str:
        """Decrypt and return raw content string."""
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:BLOCK_SIZE])
        raw = cipher.decrypt(base64.b64decode(encrypt_text))
        pad = raw[-1]
        raw = raw[:-pad]
        content_len = struct.unpack("!I", raw[16:20])[0]
        content = raw[20:20 + content_len].decode("utf-8")
        app_key = raw[20 + content_len:].decode("utf-8")
        if app_key != self.app_key:
            raise ValueError(f"AppKey mismatch: expected {self.app_key}, got {app_key}")
        return content

    def decrypt_json(self, encrypt_text: str) -> dict[str, Any]:
        """Decrypt and parse as JSON."""
        return json.loads(self.decrypt(encrypt_text))

    def encrypt(self, plain_text: str) -> tuple[str, str, str, str]:
        plain_bytes = plain_text.encode("utf-8")
        random_bytes = bytes(random.getrandbits(8) for _ in range(16))
        msg_len = struct.pack("!I", len(plain_bytes))
        corp_bytes = self.app_key.encode("utf-8")
        raw = random_bytes + msg_len + plain_bytes + corp_bytes
        pad = BLOCK_SIZE - len(raw) % BLOCK_SIZE
        raw += bytes([pad] * pad)
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:BLOCK_SIZE])
        encrypted = base64.b64encode(cipher.encrypt(raw)).decode()
        timestamp = str(int(time.time() * 1000))
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        msg_signature = self._sha1_sign(self.token, timestamp, nonce, encrypted)
        return encrypted, msg_signature, timestamp, nonce

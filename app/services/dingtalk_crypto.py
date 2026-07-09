from __future__ import annotations

import hashlib


class DingTalkCallbackCrypto:
    def __init__(self, *, token: str, aes_key: str, app_key: str):
        self.token = token or ""
        self.aes_key = aes_key or ""
        self.app_key = app_key or ""

    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        parts = sorted([self.token, timestamp or "", nonce or "", encrypt or ""])
        digest = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        return digest == (signature or "")

    def decrypt(self, encrypt: str) -> str:
        raise RuntimeError("DingTalk encrypted callback decrypt is not configured in this local package.")

    def encrypt(self, plain: str) -> tuple[str, str, str, str]:
        raise RuntimeError("DingTalk encrypted callback encrypt is not configured in this local package.")

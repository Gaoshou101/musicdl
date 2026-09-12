import base64
import binascii
import hashlib
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def compute_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    return hashlib.sha1("".join(sorted((token, timestamp, nonce, encrypted))).encode()).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, encrypted: str, signature: str) -> None:
    if not hmac.compare_digest(compute_signature(token, timestamp, nonce, encrypted), signature.lower()):
        raise WeComCryptoError("invalid_signature")


class WeComCrypto:
    def __init__(self, encoding_aes_key: str, corp_id: str):
        try:
            raw = base64.b64decode(encoding_aes_key + "=", validate=True)
        except (ValueError, binascii.Error):
            raise WeComCryptoError("invalid_aes_key") from None
        if len(raw) != 32:
            raise WeComCryptoError("invalid_aes_key")
        self._key, self._corp_id = raw, corp_id

    def encrypt(self, message: str, *, random_prefix: bytes | None = None, receive_id: str | None = None) -> str:
        prefix = random_prefix if random_prefix is not None else __import__("secrets").token_bytes(16)
        if len(prefix) != 16:
            raise WeComCryptoError("invalid_random_prefix")
        try:
            message_bytes = message.encode("utf-8")
            receive_bytes = (receive_id or self._corp_id).encode("utf-8")
        except UnicodeEncodeError:
            raise WeComCryptoError("invalid_utf8") from None
        body = prefix + struct.pack(">I", len(message_bytes)) + message_bytes + receive_bytes
        pad = 32 - len(body) % 32
        body += bytes([pad]) * pad
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).encryptor()
        return base64.b64encode(cipher.update(body) + cipher.finalize()).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        try:
            raw = base64.b64decode(encrypted, validate=True)
        except (ValueError, binascii.Error):
            raise WeComCryptoError("invalid_base64") from None
        if not raw or len(raw) % 16:
            raise WeComCryptoError("invalid_ciphertext")
        try:
            dec = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
            body = dec.update(raw) + dec.finalize()
            pad = body[-1]
            if not 1 <= pad <= 32 or body[-pad:] != bytes([pad]) * pad:
                raise WeComCryptoError("invalid_padding")
            body = body[:-pad]
            if len(body) < 20:
                raise WeComCryptoError("invalid_plaintext")
            size = struct.unpack(">I", body[16:20])[0]
            if size > len(body) - 20:
                raise WeComCryptoError("invalid_plaintext")
            if size > 32 * 1024:
                raise WeComCryptoError("plaintext_too_large")
            msg = body[20:20 + size]
            receive = body[20 + size:].decode("utf-8")
            if receive != self._corp_id:
                raise WeComCryptoError("invalid_receiveid")
            return msg.decode("utf-8")
        except UnicodeDecodeError:
            raise WeComCryptoError("invalid_plaintext") from None

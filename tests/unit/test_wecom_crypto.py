import base64

import pytest

from musicdl.wecom.crypto import WeComCrypto, WeComCryptoError, compute_signature, verify_signature


TOKEN = "token"
KEY = "a" * 43
CORP = "ww-test"


def test_signature_is_sorted_sha1_and_tampering_rejected():
    signature = compute_signature(TOKEN, "1700000000", "nonce", "cipher")
    assert signature == "8c6f84ef413f28c2cc9d769d230086a1a493b47c"  # self cross-vector
    verify_signature(TOKEN, "1700000000", "nonce", "cipher", signature)
    with pytest.raises(WeComCryptoError) as exc:
        verify_signature(TOKEN, "1700000000", "nonce", "cipher", "0" * 40)
    assert exc.value.code == "invalid_signature"


def test_encrypt_decrypt_uses_utf8_byte_length_and_receiveid():
    crypto = WeComCrypto(KEY, CORP)
    encrypted = crypto.encrypt("你好", random_prefix=b"0123456789abcdef")
    assert crypto.decrypt(encrypted) == "你好"
    assert crypto.decrypt(base64.b64encode(base64.b64decode(encrypted)).decode()) == "你好"


@pytest.mark.parametrize("payload,code", [("!", "invalid_base64"), (base64.b64encode(b"short").decode(), "invalid_ciphertext")])
def test_invalid_ciphertext_has_stable_error(payload, code):
    with pytest.raises(WeComCryptoError) as exc:
        WeComCrypto(KEY, CORP).decrypt(payload)
    assert exc.value.code == code


def test_rejects_bad_key():
    with pytest.raises(WeComCryptoError) as exc:
        WeComCrypto("bad", CORP)
    assert exc.value.code == "invalid_aes_key"


def test_rejects_bad_padding():
    crypto = WeComCrypto(KEY, CORP)
    encrypted = bytearray(base64.b64decode(crypto.encrypt("ok", random_prefix=b"0123456789abcdef")))
    encrypted[-1] ^= 1
    with pytest.raises(WeComCryptoError) as exc:
        crypto.decrypt(base64.b64encode(encrypted).decode())
    assert exc.value.code == "invalid_padding"


@pytest.mark.parametrize("message", ["\ud800"])
def test_rejects_invalid_utf8_message(message):
    with pytest.raises(WeComCryptoError) as exc:
        WeComCrypto(KEY, CORP).encrypt(message, random_prefix=b"0123456789abcdef")
    assert exc.value.code == "invalid_utf8"


def test_rejects_forged_plaintext_length_and_short_plaintext():
    crypto = WeComCrypto(KEY, CORP)
    encrypted = crypto.encrypt("ok", random_prefix=b"0123456789abcdef")
    raw = bytearray(base64.b64decode(encrypted))
    raw[-1] ^= 1
    with pytest.raises(WeComCryptoError):
        crypto.decrypt(base64.b64encode(raw).decode())
    with pytest.raises(WeComCryptoError) as exc:
        crypto.decrypt(base64.b64encode(b"\x00" * 16).decode())
    assert exc.value.code == "invalid_padding"
    crypto = WeComCrypto(KEY, CORP)
    encrypted = crypto.encrypt("ok", random_prefix=b"0123456789abcdef", receive_id="other")
    with pytest.raises(WeComCryptoError) as exc:
        crypto.decrypt(encrypted)
    assert exc.value.code == "invalid_receiveid"

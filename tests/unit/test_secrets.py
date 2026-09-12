from pydantic import SecretStr

from musicdl.secrets import redact_secrets


def test_redact_secrets_recurses_through_mapping_and_sequences():
    value = {
        "username": "alice",
        "api_key": "key-value",
        "nested": [{"Authorization": "Bearer abc"}, {"session": "session-data"}],
        "safe": {"name": "ok"},
    }
    result = redact_secrets(value)
    assert result == {
        "username": "alice",
        "api_key": "[REDACTED_SECRET]",
        "nested": [{"Authorization": "[REDACTED_SECRET]"}, {"session": "[REDACTED_SECRET]"}],
        "safe": {"name": "ok"},
    }


def test_redact_secrets_masks_secretstr_and_sensitive_key_names_case_insensitively():
    result = redact_secrets({"PASSWORD": SecretStr("pw"), "value": SecretStr("visible")})
    assert result["PASSWORD"] == "[REDACTED_SECRET]"
    assert result["value"] == "[REDACTED_SECRET]"

def test_redact_secrets_masks_aes_key_and_private_key_names():
    result = redact_secrets({"encoding_aes_key": "raw_aes", "rsa_private_key": "raw_pk"})
    assert result["encoding_aes_key"] == "[REDACTED_SECRET]"
    assert result["rsa_private_key"] == "[REDACTED_SECRET]"

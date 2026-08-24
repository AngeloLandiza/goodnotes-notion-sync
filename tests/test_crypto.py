"""Credential encryption. The failure mode is other people's accounts."""

import pytest

from goodnotes_notion_sync.crypto import CryptoError, SecretBox, generate_key


def test_round_trip():
    box = SecretBox([generate_key()])
    assert box.decrypt(box.encrypt("ntn_secret")) == "ntn_secret"


def test_the_ciphertext_does_not_contain_the_plaintext():
    box = SecretBox([generate_key()])
    assert b"ntn_secret" not in box.encrypt("ntn_secret")


def test_no_key_means_refuse_rather_than_store_plaintext():
    """There is deliberately no fallback.

    A silent plaintext mode is the kind of thing that survives to production
    and is discovered by someone else.
    """
    with pytest.raises(CryptoError) as exc:
        SecretBox([])
    assert "plaintext" in str(exc.value)


def test_from_env_refuses_when_the_key_is_absent():
    with pytest.raises(CryptoError):
        SecretBox.from_env({})


def test_optional_returns_none_instead_of_raising():
    assert SecretBox.optional({}) is None
    assert SecretBox.optional({"APP_ENCRYPTION_KEY": generate_key()}) is not None


def test_a_nonsense_key_is_a_config_error_not_a_traceback():
    with pytest.raises(CryptoError) as exc:
        SecretBox(["not-a-fernet-key"])
    assert "keygen" in str(exc.value)


def test_another_key_cannot_decrypt():
    blob = SecretBox([generate_key()]).encrypt("secret")
    with pytest.raises(CryptoError):
        SecretBox([generate_key()]).decrypt(blob)


def test_the_old_key_still_decrypts_after_rotation():
    """Rotating APP_ENCRYPTION_KEY must not log everybody out of everything."""
    old, new = generate_key(), generate_key()
    blob = SecretBox([old]).encrypt("secret")

    rotated = SecretBox([new, old])
    assert rotated.decrypt(blob) == "secret"
    # New writes use the new key, and the old box can no longer read them.
    fresh = rotated.encrypt("secret")
    with pytest.raises(CryptoError):
        SecretBox([old]).decrypt(fresh)


def test_the_rotation_error_says_what_to_do():
    blob = SecretBox([generate_key()]).encrypt("secret")
    with pytest.raises(CryptoError) as exc:
        SecretBox([generate_key()]).decrypt(blob)
    assert "APP_ENCRYPTION_KEY_OLD" in str(exc.value)


def test_empty_input_decrypts_to_empty():
    box = SecretBox([generate_key()])
    assert box.decrypt(None) == ""
    assert box.decrypt(b"") == ""

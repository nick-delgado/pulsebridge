"""The mounted session-signing-key file: what counts as a key, the cache, rotation."""

from __future__ import annotations

import pytest
from fhir_proxy.signing_key import KeyUnavailable, SigningKeyFile


class Mono:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def read(tmp_path, data: bytes) -> bytes:  # noqa: ANN001
    path = tmp_path / "key"
    path.write_bytes(data)
    return SigningKeyFile(str(path)).current()


def test_thirty_two_raw_bytes_are_a_key(tmp_path) -> None:
    raw = bytes(range(32))
    assert read(tmp_path, raw) == raw


def test_a_raw_key_ending_in_a_newline_byte_keeps_it(tmp_path) -> None:
    raw = bytes(range(31)) + b"\n"
    assert read(tmp_path, raw) == raw


def test_a_text_encoding_loses_one_trailing_newline(tmp_path) -> None:
    text = b"q" * 44  # e.g. base64 of 32 bytes
    assert read(tmp_path, text + b"\n") == text
    assert read(tmp_path, text + b"\r\n") == text


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "too-short"),
        (b"x" * 31, "too-short"),
        (b"x" * 30 + b"\n", "too-short"),
        (b"x" * 1025, "unreadable"),
    ],
)
def test_unusable_content(tmp_path, data: bytes, code: str) -> None:
    with pytest.raises(KeyUnavailable) as raised:
        read(tmp_path, data)
    assert str(raised.value) == code


def test_a_missing_file(tmp_path) -> None:
    with pytest.raises(KeyUnavailable, match="missing"):
        SigningKeyFile(str(tmp_path / "absent")).current()


def test_a_new_version_is_seen_after_the_cache_time(tmp_path) -> None:
    path = tmp_path / "key"
    path.write_bytes(b"a" * 32)
    mono = Mono()
    keys = SigningKeyFile(str(path), cache_seconds=30, monotonic=mono)
    assert keys.current() == b"a" * 32
    path.write_bytes(b"b" * 32)
    mono.now = 29.9
    assert keys.current() == b"a" * 32
    mono.now = 30.0
    assert keys.current() == b"b" * 32


def test_a_failure_is_not_cached(tmp_path) -> None:
    path = tmp_path / "key"
    keys = SigningKeyFile(str(path))
    with pytest.raises(KeyUnavailable):
        keys.current()
    path.write_bytes(b"c" * 32)
    assert keys.current() == b"c" * 32

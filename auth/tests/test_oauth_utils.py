"""Tests for auth.oauth_utils PKCE/state/hash helpers."""

from __future__ import annotations

import re

from auth.oauth_utils import (
    generate_opaque_secret,
    generate_pkce_pair,
    generate_state,
    sha256_b64u,
    verify_pkce_s256,
)


class TestPKCE:
    def test_round_trip_succeeds(self):
        for _ in range(5):
            verifier, challenge = generate_pkce_pair()
            assert verify_pkce_s256(code_verifier=verifier, code_challenge=challenge)

    def test_wrong_verifier_fails(self):
        verifier, challenge = generate_pkce_pair()
        assert not verify_pkce_s256(code_verifier=verifier + "x", code_challenge=challenge)

    def test_challenge_is_base64url_unpadded(self):
        _, challenge = generate_pkce_pair()
        assert re.fullmatch(r"[A-Za-z0-9_-]+", challenge)
        assert not challenge.endswith("=")

    def test_verifier_is_random(self):
        a, _ = generate_pkce_pair()
        b, _ = generate_pkce_pair()
        assert a != b


class TestState:
    def test_state_is_url_safe(self):
        for _ in range(5):
            state = generate_state()
            assert re.fullmatch(r"[A-Za-z0-9_-]+", state)
            # 32 bytes -> base64url is ~43 chars
            assert len(state) >= 32

    def test_state_is_random(self):
        assert generate_state() != generate_state()


class TestOpaqueSecret:
    def test_secret_length_grows_with_byte_length(self):
        a = generate_opaque_secret(8)
        b = generate_opaque_secret(64)
        assert len(b) > len(a)


class TestSHA256B64U:
    def test_deterministic(self):
        assert sha256_b64u("abc") == sha256_b64u("abc")

    def test_distinct_inputs_differ(self):
        assert sha256_b64u("abc") != sha256_b64u("abcd")

    def test_url_safe_no_padding(self):
        out = sha256_b64u("x")
        assert re.fullmatch(r"[A-Za-z0-9_-]+", out)
        assert not out.endswith("=")

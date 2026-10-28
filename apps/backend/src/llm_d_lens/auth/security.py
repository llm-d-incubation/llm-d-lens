"""Credential, session-token and secret primitives for the auth domain.

Kept free of persistence and transport so it can be reused by the service
layer and unit-tested in isolation. Design references: sections 5.2, 5.3, 8.3
and 12.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable, Sequence

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

SESSION_TOKEN_BYTES = 32
INTERNAL_AUTH_WINDOW_SECONDS = 60
ACCESS_TOKEN_ISSUER = "lens"  # noqa: S105 - issuer name, not a credential
ACCESS_TOKEN_LEEWAY_SECONDS = 30

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password with Argon2id (the library default profile)."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time password verification; never raises on mismatch."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, ValueError):
        return False


def generate_session_token() -> str:
    """Generate a new opaque session token (only its hash is persisted)."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """SHA-256 hash used for session-token lookup in the ``sessions`` table."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _internal_message(timestamp: int, method: str, path: str, principal_id: str) -> bytes:
    return f"{timestamp}\n{method.upper()}\n{path}\n{principal_id}".encode()


def sign_internal(secret: str, *, timestamp: int, method: str, path: str, principal_id: str) -> str:
    """Sign the Node->Python principal assertion (design section 8.3)."""
    return hmac.new(
        secret.encode("utf-8"),
        _internal_message(timestamp, method, path, principal_id),
        hashlib.sha256,
    ).hexdigest()


def verify_internal(
    secret: str,
    *,
    signature: str,
    timestamp: int,
    method: str,
    path: str,
    principal_id: str,
    now: int | None = None,
    window_seconds: int = INTERNAL_AUTH_WINDOW_SECONDS,
) -> bool:
    """Verify signature freshness and integrity in constant time."""
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > window_seconds:
        return False
    expected = sign_internal(secret, timestamp=timestamp, method=method, path=path, principal_id=principal_id)
    return hmac.compare_digest(expected, signature)


def _fernet_for(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


class SecretCipher:
    """Symmetric encryption for stored secrets with key-rotation fallback.

    ``primary`` encrypts new values; ``old`` keys are tried on decrypt so a key
    can be rotated without re-encrypting everything at once (design section
    12). Raises ``ValueError`` when no key can decrypt.
    """

    def __init__(self, primary: str, old: Iterable[str] = ()):  # noqa: S107 - secret passed by caller
        if not primary:
            raise ValueError("a non-empty primary secret key is required")
        self._primary = _fernet_for(primary)
        self._fallbacks = [_fernet_for(key) for key in old if key]

    def encrypt(self, plaintext: str) -> str:
        return self._primary.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        for fernet in (self._primary, *self._fallbacks):
            try:
                return fernet.decrypt(token.encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError):
                continue
        raise ValueError("ciphertext could not be decrypted with any configured key")


class AccessTokenError(ValueError):
    """Raised when a Bearer access token is missing, malformed or expired."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def encode_access_token(
    secret: str,
    *,
    user_id: str,
    session_id: str,
    audience: str,
    authz_version: int,
    ttl_seconds: int,
    issuer: str = ACCESS_TOKEN_ISSUER,
    now: int | None = None,
) -> tuple[str, int]:
    """Create a compact HS256 JWT access token and return ``(token, expires_in)``."""
    issued_at = int(time.time()) if now is None else int(now)
    ttl = max(int(ttl_seconds), 1)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": issuer,
        "sub": user_id,
        "sid": session_id,
        "aud": audience,
        "authzVersion": int(authz_version),
        "iat": issued_at,
        "exp": issued_at + ttl,
    }
    signing_input = ".".join(
        (
            _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")),
            _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")),
        )
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}", ttl


def decode_access_token(
    token: str,
    secrets: Sequence[str],
    *,
    audience: str,
    issuer: str = ACCESS_TOKEN_ISSUER,
    now: int | None = None,
    leeway_seconds: int = ACCESS_TOKEN_LEEWAY_SECONDS,
) -> dict[str, object]:
    """Validate an HS256 access token and return its claims.

    ``secrets`` is ordered (current key first, then rotation fallbacks). All
    signatures are checked in constant time; the first matching key wins.
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise AccessTokenError("access token is malformed")
    signing_input = f"{parts[0]}.{parts[1]}"
    try:
        signature = _b64url_decode(parts[2])
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as error:
        raise AccessTokenError("access token is malformed") from error
    if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") not in {None, "JWT"}:
        raise AccessTokenError("access token algorithm is not accepted")
    if not isinstance(claims, dict):
        raise AccessTokenError("access token claims are invalid")
    keys = [key for key in secrets if key]
    if not keys:
        raise AccessTokenError("access token signing key is unavailable")
    if not any(
        hmac.compare_digest(
            hmac.new(key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest(),
            signature,
        )
        for key in keys
    ):
        raise AccessTokenError("access token signature is invalid")
    if claims.get("iss") != issuer:
        raise AccessTokenError("access token issuer is invalid")
    if claims.get("aud") != audience:
        raise AccessTokenError("access token audience is invalid")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AccessTokenError("access token subject is missing")
    if not isinstance(claims.get("sid"), str) or not claims["sid"]:
        raise AccessTokenError("access token session is missing")
    current = int(time.time()) if now is None else int(now)
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int) or current > exp + max(leeway_seconds, 0):
        raise AccessTokenError("access token is expired")
    if isinstance(iat, int) and iat > current + max(leeway_seconds, 0):
        raise AccessTokenError("access token was issued in the future")
    return claims

"""User model-access-token lifecycle: issue, list, reset, revoke, validate.

Reuses the auth domain's token primitives (``generate_session_token`` /
``hash_session_token``) so model tokens and login sessions share one random/hash
implementation, while living in a separate table and namespace.

Design reference: docs/design/model-service-design.md#6-domain-model
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta

from llm_d_lens.auth.master_key import ensure_master_key, resolve_master_key
from llm_d_lens.auth.security import generate_session_token, hash_session_token
from llm_d_lens.auth.settings import get_settings
from llm_d_lens.db.dao.model_access_token import ModelAccessTokenDao
from llm_d_lens.model_service.contracts import DELEGATED_TOKEN_PREFIX, TOKEN_PREFIX, ModelAccessToken, utcnow

#: Re-touch ``last_used_at`` at most this often, to avoid a write per request.
LAST_USED_TOUCH_SECONDS = 60

_DELEGATED_PURPOSE = "lens.model_token.delegated.v1"
DEFAULT_DELEGATED_RUN_TTL_SECONDS = 24 * 60 * 60
MAX_DELEGATED_RUN_TTL_SECONDS = 7 * 24 * 60 * 60
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes | None:
    if not segment or not _B64URL_RE.fullmatch(segment):
        return None
    padded = segment + ("=" * ((-len(segment)) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError):
        return None
    if _b64url_encode(decoded) != segment:
        return None
    return decoded


def generate_model_token() -> str:
    """Generate a new opaque ``lens-mk-…`` plaintext token (43-char body)."""
    return f"{TOKEN_PREFIX}{generate_session_token()}"


def hash_model_token(raw_token: str) -> str:
    return hash_session_token(raw_token)


def token_hint(raw_token: str) -> str:
    """Short, non-secret fingerprint: the last 4 characters of the token."""
    return raw_token[-4:]


class ModelTokenService:
    def __init__(self, dao: ModelAccessTokenDao | None = None) -> None:
        self._dao = dao or ModelAccessTokenDao()

    def list_for_user(self, user_id: str, *, include_revoked: bool = True) -> list[ModelAccessToken]:
        if include_revoked:
            return self._dao.list_for_user(user_id)
        return self._dao.list_for_user(user_id, status="active")

    def get(self, token_id: str) -> ModelAccessToken | None:
        return self._dao.get(token_id)

    def create(self, user_id: str, name: str = "default") -> tuple[ModelAccessToken, str]:
        """Issue a new token; returns the record and the one-time plaintext."""
        raw = generate_model_token()
        token = ModelAccessToken(
            user_id=user_id,
            name=name,
            token_hash=hash_model_token(raw),
            token_hint=token_hint(raw),
        )
        return self._dao.create(token), raw

    def regenerate(
        self, user_id: str, token_id: str | None = None, name: str = "default"
    ) -> tuple[ModelAccessToken, str]:
        """Revoke the user's active token(s) and issue a fresh one.

        With ``token_id`` only that token is revoked; otherwise every active
        token is revoked ("one primary token per user").
        """
        now = utcnow()
        targets = self._dao.list_for_user(user_id, status="active")
        if token_id is not None:
            targets = [token for token in targets if token.id == token_id]
        for token in targets:
            token.status = "revoked"
            token.revoked_at = now
            self._dao.save(token)
        return self.create(user_id, name=name)

    def revoke(self, user_id: str, token_id: str) -> bool:
        token = self._dao.get(token_id)
        if token is None or token.user_id != user_id:
            return False
        if token.status == "revoked":
            return True
        token.status = "revoked"
        token.revoked_at = utcnow()
        self._dao.save(token)
        return True

    def delete(self, user_id: str, token_id: str) -> bool:
        token = self._dao.get(token_id)
        if token is None or token.user_id != user_id:
            return False
        self._dao.delete(token_id)
        return True

    def require_active_owned_token(self, user_id: str, token_id: str) -> ModelAccessToken:
        """Verify that ``token_id`` exists, belongs to ``user_id``, is active, and has not expired."""
        if not user_id or not token_id:
            raise ValueError("Selected model access token was not found or is inactive.")
        record = self._dao.get(token_id)
        if record is None or record.user_id != user_id or record.status != "active":
            raise ValueError("Selected model access token was not found or is inactive.")
        expires_at = _as_utc(record.expires_at)
        if expires_at is not None and utcnow() >= expires_at:
            raise ValueError("Selected model access token has expired.")
        return record

    def mint_delegated_run_token(
        self,
        user_id: str,
        token_id: str,
        *,
        ttl_seconds: int = DEFAULT_DELEGATED_RUN_TTL_SECONDS,
    ) -> str:
        """Mint a short-lived HMAC-signed delegated token bound to ``token_id`` and ``user_id``."""
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")
        clamped_ttl = min(ttl_seconds, MAX_DELEGATED_RUN_TTL_SECONDS)
        record = self.require_active_owned_token(user_id, token_id)
        master = ensure_master_key(get_settings())
        if not master.primary:
            raise RuntimeError("Master key is unconfigured")
        now = utcnow()
        exp_dt = now + timedelta(seconds=clamped_ttl)
        record_expires_at = _as_utc(record.expires_at)
        if record_expires_at is not None and record_expires_at < exp_dt:
            exp_dt = record_expires_at
        iat = int(now.timestamp())
        exp = int(exp_dt.timestamp())
        if exp <= iat:
            raise ValueError("Selected model access token has expired.")
        payload = {
            "v": 1,
            "typ": "delegated_run",
            "tid": record.id,
            "uid": record.user_id,
            "iat": iat,
            "exp": exp,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = _b64url_encode(payload_bytes)
        signing_input = f"{_DELEGATED_PURPOSE}.{payload_b64}".encode("ascii")
        digest = hmac.new(master.primary.encode("utf-8"), signing_input, hashlib.sha256).digest()
        sig_b64 = _b64url_encode(digest)
        return f"{DELEGATED_TOKEN_PREFIX}{payload_b64}.{sig_b64}"

    def validate(self, raw_token: str, *, ip: str | None = None) -> ModelAccessToken | None:
        """Return the active token for ``raw_token``, or ``None`` if unusable."""
        if not raw_token:
            return None
        if raw_token.startswith(DELEGATED_TOKEN_PREFIX):
            return self._validate_delegated(raw_token, ip=ip)
        token = self._dao.get_by_hash(hash_model_token(raw_token))
        if token is None or token.status != "active":
            return None
        now = utcnow()
        expires_at = _as_utc(token.expires_at)
        if expires_at is not None and now >= expires_at:
            return None
        self._touch(token, now=now, ip=ip)
        return token

    def _validate_delegated(self, raw_token: str, *, ip: str | None = None) -> ModelAccessToken | None:
        parts = raw_token.split(".")
        if len(parts) != 3:
            return None
        prefix, payload_b64, sig_b64 = parts
        if f"{prefix}." != DELEGATED_TOKEN_PREFIX or not payload_b64 or not sig_b64:
            return None
        provided_digest = _b64url_decode(sig_b64)
        if provided_digest is None or len(provided_digest) != 32:
            return None
        payload_bytes = _b64url_decode(payload_b64)
        if payload_bytes is None:
            return None

        master = resolve_master_key(get_settings())
        keys = [key for key in (master.primary, *master.old) if key]
        if not keys:
            return None
        signing_input = f"{_DELEGATED_PURPOSE}.{payload_b64}".encode("ascii")
        if not any(
            hmac.compare_digest(
                hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest(),
                provided_digest,
            )
            for key in keys
        ):
            return None

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("v")
        if isinstance(version, bool) or version != 1:
            return None
        if payload.get("typ") != "delegated_run":
            return None
        tid = payload.get("tid")
        uid = payload.get("uid")
        iat = payload.get("iat")
        exp = payload.get("exp")
        if not isinstance(tid, str) or not tid or not isinstance(uid, str) or not uid:
            return None
        if isinstance(iat, bool) or not isinstance(iat, int):
            return None
        if isinstance(exp, bool) or not isinstance(exp, int):
            return None
        now = utcnow()
        if now.timestamp() >= exp:
            return None

        record = self._dao.get(tid)
        if record is None or record.user_id != uid or record.status != "active":
            return None
        record_expires_at = _as_utc(record.expires_at)
        if record_expires_at is not None and now >= record_expires_at:
            return None
        self._touch(record, now=now, ip=ip)
        return record

    def _touch(self, token: ModelAccessToken, *, now: datetime, ip: str | None) -> None:
        last_used = _as_utc(token.last_used_at)
        stale = last_used is None or (now - last_used) > timedelta(seconds=LAST_USED_TOUCH_SECONDS)
        if not stale:
            return
        token.last_used_at = now
        if ip:
            token.last_used_ip = ip
        self._dao.save(token)

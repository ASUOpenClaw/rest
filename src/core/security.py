"""
JWT, API key, and password-hashing utilities.

JWT payload fields:
  sub  – user UUID (str)
  jti  – unique token id (str UUID)
  type – "access" | "refresh"
  exp  – expiry (int unix timestamp)
  iat  – issued at (int unix timestamp)

Refresh tokens are stored in Redis under key: rt:<user_id>:<jti>
Value is "1"; TTL matches token lifetime.

API key format: lab_sk_<32 random hex bytes>
Stored as:
  key_prefix = first 8 chars (after prefix)
  key_hash   = bcrypt hash of full key string
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from .config import settings

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

_ACCESS_TYPE = "access"
_REFRESH_TYPE = "refresh"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def create_access_token(user_id: uuid.UUID) -> tuple[str, str]:
    """Return (encoded_jwt, jti)."""
    jti = str(uuid.uuid4())
    now = _now_utc()
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": _ACCESS_TYPE,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()
        ),
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm=settings.algorithm)
    return token, jti


def create_refresh_token(user_id: uuid.UUID) -> tuple[str, str]:
    """Return (encoded_jwt, jti)."""
    jti = str(uuid.uuid4())
    now = _now_utc()
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": _REFRESH_TYPE,
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(days=settings.refresh_token_expire_days)).timestamp()
        ),
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm=settings.algorithm)
    return token, jti


def decode_token(token: str) -> dict:
    """
    Decode and validate a JWT.
    Raises jose.JWTError on any validation failure.
    """
    return jwt.decode(token, settings.jwt_public_key, algorithms=[settings.algorithm])


def redis_refresh_key(user_id: str | uuid.UUID, jti: str) -> str:
    return f"rt:{user_id}:{jti}"


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

_API_KEY_PREFIX = "lab_sk_"
_API_KEY_RANDOM_BYTES = 32


def generate_api_key() -> str:
    """Generate a new plaintext API key."""
    return _API_KEY_PREFIX + secrets.token_hex(_API_KEY_RANDOM_BYTES)


def api_key_prefix(key: str) -> str:
    """Return the 8-char lookup prefix (the random part, not the constant prefix)."""
    return key[len(_API_KEY_PREFIX) : len(_API_KEY_PREFIX) + 8]


def hash_api_key(key: str) -> str:
    """Return bcrypt hash of the full key string."""
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()


def verify_api_key(key: str, key_hash: str) -> bool:
    """Constant-time bcrypt verification."""
    return bcrypt.checkpw(key.encode(), key_hash.encode())


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

"""Nhost JWT authentication."""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthCredentials
import jwt
from jwt import PyJWKClient

from api.config import settings

_jwks_url = (
    settings.nhost_jwks_url
    or f"https://{settings.nhost_subdomain}.auth.{settings.nhost_region}.nhost.run/v1/.well-known/jwks.json"
)
_jwks_client: PyJWKClient | None = None

def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_jwks_url)
    return _jwks_client

_security = HTTPBearer(auto_error=False)

def get_current_user(credentials: HTTPAuthCredentials | None = Depends(_security)) -> str:
    """Extract user_id from Nhost JWT. Returns the Nhost user UUID."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(credentials.credentials)
        payload = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: no sub")
        return user_id
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    except Exception as e:
        raise HTTPException(status_code=503, detail="Authentication service unavailable")

def validate_ws_token(token: str) -> str:
    """Validate JWT from WebSocket query param. Returns user_id or raises."""
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("No sub in token")
        return user_id
    except Exception as e:
        raise ValueError(f"Invalid token: {e}")

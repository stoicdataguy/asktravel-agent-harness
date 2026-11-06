"""Bearer-token check on the agent API (#27).

The API trusts the X-OpenWebUI-User-Email header to say who the user is, so only Open WebUI may
call it: every route except /health requires `Authorization: Bearer <AGENT_API_KEY>`, the key
Open WebUI sends as its OPENAI_API_KEY. With no key configured the check is off (local dev).
"""

import logging
import secrets

from fastapi import HTTPException, Request

from travel_agent.config import get_settings

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health"}


def _key() -> str:
    return get_settings().agent_api_key.get_secret_value()


async def require_api_key(request: Request) -> None:
    key = _key()
    if not key or request.url.path in OPEN_PATHS:
        return
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), key.encode()):
        raise HTTPException(
            401, detail="Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"}
        )


def check_auth_config() -> None:
    if not _key():
        logger.warning(
            "AGENT_API_KEY is not set: the API accepts requests without a token. "
            "Fine on a laptop, never in production."
        )


def api_key_headers() -> dict[str, str]:
    """Headers for scripts that call the API directly."""
    key = _key()
    return {"authorization": f"Bearer {key}"} if key else {}

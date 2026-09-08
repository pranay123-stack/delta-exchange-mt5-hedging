"""Role-based access control and dangerous-action confirmation.

Three roles, checked on every mutating endpoint:

* ``VIEWER``  -- read only.
* ``TRADER``  -- may execute paper hedges, rebalance, run scenarios.
* ``ADMIN``   -- may edit configuration, inject faults, use the kill switch.

Authentication is by API key header.  Keys are supplied through the environment
and hashed before comparison; none are committed.  When no keys are configured
the platform runs open in the ``local`` environment and **refuses to start
unauthenticated anywhere else** -- a demo default must not become a production
hole.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

API_KEY_HEADER = "X-API-Key"
CONFIRM_HEADER = "X-Confirm-Action"


class Role(str, Enum):
    VIEWER = "VIEWER"
    TRADER = "TRADER"
    ADMIN = "ADMIN"

    @property
    def rank(self) -> int:
        return [Role.VIEWER, Role.TRADER, Role.ADMIN].index(self)

    def satisfies(self, required: Role) -> bool:
        return self.rank >= required.rank


@dataclass(frozen=True, slots=True)
class Principal:
    username: str
    role: Role

    @property
    def is_anonymous(self) -> bool:
        return self.username == "anonymous"


ANONYMOUS_ADMIN = Principal(username="anonymous", role=Role.ADMIN)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _configured_keys() -> dict[str, Principal]:
    """Read ``HEDGELAB_API_KEYS`` as ``user:role:key`` triples, comma separated.

    Only the hash is retained, so a memory dump of the running process does not
    reveal usable keys.
    """
    raw = os.environ.get("HEDGELAB_API_KEYS", "").strip()
    if not raw:
        return {}
    keys: dict[str, Principal] = {}
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) != 3:
            log.error("ignoring malformed API key entry (expected user:role:key)")
            continue
        username, role_name, key = (p.strip() for p in parts)
        try:
            role = Role(role_name.upper())
        except ValueError:
            log.error("ignoring API key with unknown role", extra={"role": role_name})
            continue
        keys[_hash(key)] = Principal(username=username, role=role)
    return keys


def resolve_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> Principal:
    """Identify the caller.

    With no keys configured the platform is open -- acceptable for a local
    paper-trading demo, and refused outside ``local``/``test``.
    """
    configured = _configured_keys()
    if not configured:
        if settings.environment not in ("local", "test"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"no API keys configured but environment is {settings.environment!r}; "
                    f"set HEDGELAB_API_KEYS before running outside local"
                ),
            )
        return ANONYMOUS_ADMIN

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {API_KEY_HEADER} header",
        )
    digest = _hash(api_key)
    for known, principal in configured.items():
        if hmac.compare_digest(known, digest):   # constant time
            return principal
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


CurrentPrincipal = Annotated[Principal, Depends(resolve_principal)]


def require_role(required: Role) -> Callable[[Principal], Principal]:
    """Dependency enforcing a minimum role."""

    def _dependency(principal: CurrentPrincipal) -> Principal:
        if not principal.role.satisfies(required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {principal.role.value} cannot perform this action; "
                       f"{required.value} required",
            )
        return principal

    return _dependency


RequireTrader = Annotated[Principal, Depends(require_role(Role.TRADER))]
RequireAdmin = Annotated[Principal, Depends(require_role(Role.ADMIN))]


def require_confirmation(
    settings: Annotated[Settings, Depends(get_settings)],
    confirm: Annotated[str | None, Header(alias=CONFIRM_HEADER)] = None,
) -> None:
    """Guard for irreversible actions (kill switch, flatten, adopt venue state).

    The caller must echo ``X-Confirm-Action: CONFIRM``.  It is deliberately not
    a query parameter: a confirmation that can be triggered by following a link
    is not a confirmation.
    """
    if not settings.require_confirmation_for_dangerous_actions:
        return
    if (confirm or "").strip().upper() != "CONFIRM":
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail=(
                f"this action is irreversible; resend with the header "
                f"{CONFIRM_HEADER}: CONFIRM"
            ),
        )


RequireConfirmation = Annotated[None, Depends(require_confirmation)]

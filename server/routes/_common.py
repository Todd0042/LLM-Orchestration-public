"""Helpers shared by route modules."""

from __future__ import annotations

from fastapi import HTTPException, Request


def zone(request: Request) -> str:
    return str(getattr(request.state, "zone", "local"))


def require_local(request: Request, what: str) -> None:
    """Security-sensitive changes can only be made from the machine itself (or by a peer machine)."""
    if getattr(request.state, "auth", "") == "token" or zone(request) == "local":
        return
    raise HTTPException(403, f"{what} can only be changed on the machine itself, not over {zone(request)}")

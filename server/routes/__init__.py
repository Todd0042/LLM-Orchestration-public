"""HTTP routes, one module per area. Each exposes build(ctx) -> APIRouter."""

from __future__ import annotations

from fastapi import FastAPI

from ..plugins import register_plugins
from . import auth_routes, chat_routes, internal_routes, ops_routes, orch_terminal_routes, repo_routes, system_routes, terminal_routes


def register_routes(app: FastAPI, ctx) -> None:
    for mod in (auth_routes, system_routes, repo_routes, chat_routes, terminal_routes, orch_terminal_routes, ops_routes, internal_routes):
        app.include_router(mod.build(ctx))
    register_plugins(app, ctx)


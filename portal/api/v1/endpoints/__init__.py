# SPDX-License-Identifier: Apache-2.0
"""Portal v1 endpoints package."""

from __future__ import annotations

from fastapi import APIRouter

from api.v1.endpoints import auth, cameras, edge, events

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(cameras.router)
api_router.include_router(edge.router)
api_router.include_router(events.router)

__all__: tuple[str, ...] = ("api_router",)
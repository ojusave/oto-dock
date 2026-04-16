"""Shared FastAPI router for the agent-management API package.

All ``api/agents/*`` route modules attach their handlers to this single
router, which ``app.py`` mounts (prefix-less) as ``agents.router``.
"""

from fastapi import APIRouter

router = APIRouter()

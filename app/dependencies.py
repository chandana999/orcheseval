"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.security import verify_api_key

DbSession = Annotated[Session, Depends(get_session)]
ApiKey = Annotated[str, Depends(verify_api_key)]

__all__ = ["ApiKey", "DbSession", "get_session", "verify_api_key"]

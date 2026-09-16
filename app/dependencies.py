"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from psycopg import Connection

from app.core.database import get_conn
from app.core.security import verify_api_key

DbConnection = Annotated[Connection, Depends(get_conn)]
ApiKey = Annotated[str, Depends(verify_api_key)]

__all__ = ["ApiKey", "DbConnection", "get_conn", "verify_api_key"]

"""Liveness endpoint. It reports that the API process is up; it checks no dependencies (none exist yet)."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["healthy"]


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="healthy")

import asyncio
from http import HTTPStatus

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.http.deps import get_resources

logger = structlog.stdlib.get_logger(__name__)
router = APIRouter(prefix="/health")


@router.get("/live", response_class=Response)
async def liveness() -> Response:
    return Response()


@router.get("/ready", response_class=Response)
async def readiness(request: Request) -> Response:
    engine = get_resources(request).engine
    try:
        async with asyncio.timeout(1), engine.connect() as connection:
            await connection.execute(select(1))
    except (TimeoutError, SQLAlchemyError) as error:
        logger.warning("health.not_ready", exc_info=error)
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE) from error
    return Response()

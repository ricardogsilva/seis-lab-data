from typing import AsyncGenerator

from fastapi import (
    Depends,
    Request,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from ... import config


def get_settings(request: Request) -> config.SeisLabDataSettings:
    return request.state.settings


async def get_session(
    settings: config.SeisLabDataSettings = Depends(get_settings),
) -> AsyncGenerator[AsyncSession, None]:
    session_maker = settings.get_db_session_maker()
    async with session_maker() as session:
        yield session

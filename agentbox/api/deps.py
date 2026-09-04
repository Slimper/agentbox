from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.config import Settings
from agentbox.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def get_settings_dep(runtime: Runtime = Depends(get_runtime)) -> Settings:
    return runtime.settings


async def get_session(runtime: Runtime = Depends(get_runtime)) -> AsyncIterator[AsyncSession]:
    async with runtime.db.session() as session:
        yield session

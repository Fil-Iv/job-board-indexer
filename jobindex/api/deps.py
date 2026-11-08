"""FastAPI dependencies shared by the JSON API and the HTML page."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.db import create_engine, create_session_factory


def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    """The app's session factory, created on first use when the lifespan did not run
    (serverless runtimes such as Vercel may skip ASGI lifespan events)."""
    factory: async_sessionmaker[AsyncSession] | None = getattr(app.state, "session_factory", None)
    if factory is None:
        factory = app.state.session_factory = create_session_factory(create_engine())
    return factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with session_factory(request.app)() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

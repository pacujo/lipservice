from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lipservice.routes import proxy, router


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    yield
    await proxy.shutdown()


app = FastAPI(
    title="Lipservice",
    version="0.1.0",
    description="IRC proxy with a REST API",
    lifespan=_lifespan,
)

app.include_router(router, prefix="/api")

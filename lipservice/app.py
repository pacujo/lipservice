from fastapi import FastAPI

from lipservice.routes import router

app = FastAPI(
    title="Lipservice",
    version="0.1.0",
    description="IRC proxy with a REST API",
)

app.include_router(router, prefix="/api")

import uvicorn
from lipservice.config import settings

uvicorn.run("lipservice.app:app", host=settings.host, port=settings.port)

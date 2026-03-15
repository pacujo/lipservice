import os


class Settings:
    host: str = os.environ.get("LIPSERVICE_HOST", "127.0.0.1")
    port: int = int(os.environ.get("LIPSERVICE_PORT", "8080"))
    username: str = os.environ.get("LIPSERVICE_USER", "admin")
    password: str = os.environ.get("LIPSERVICE_PASS", "changeme")
    token_lifetime_seconds: int = int(os.environ.get("LIPSERVICE_TOKEN_LIFETIME", "86400"))
    max_backlog: int = int(os.environ.get("LIPSERVICE_MAX_BACKLOG", "1000"))
    storage_backend: str = os.environ.get("LIPSERVICE_STORAGE", "memory")
    database_uri: str = os.environ.get("LIPSERVICE_DATABASE_URI", "")


settings: Settings = Settings()

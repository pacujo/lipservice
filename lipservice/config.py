import os


class Settings:
    host = os.environ.get("LIPSERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("LIPSERVICE_PORT", "8080"))
    username = os.environ.get("LIPSERVICE_USER", "admin")
    password = os.environ.get("LIPSERVICE_PASS", "changeme")
    token_lifetime_seconds = int(os.environ.get("LIPSERVICE_TOKEN_LIFETIME", "86400"))
    max_backlog = int(os.environ.get("LIPSERVICE_MAX_BACKLOG", "1000"))


settings = Settings()

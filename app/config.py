from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    whatsapp_app_secret: str

    # Password is embedded in the URL (redis://:{password}@redis:6379/0) — docker-compose
    # interpolates REDIS_PASSWORD into this value, the app itself only needs the one var.
    redis_url: str = "redis://redis:6379/0"

    # Key under which inbound jobs are pushed for the worker to pop (see app/webhook.py, worker.py).
    booking_jobs_key: str = "booking:jobs"
    # Dedupe entries expire after this long — well past Meta's own webhook retry window.
    message_dedupe_ttl_seconds: int = 86400


settings = Settings()

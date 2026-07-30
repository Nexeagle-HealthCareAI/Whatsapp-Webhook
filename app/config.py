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

    # easyHMSAPI's public surface (PublicController) — same VM, reached via its own public
    # domain rather than the docker network, since it's a separate compose project/host network.
    hms_api_base_url: str = "https://1hms-dev-api.nexeagle.com"
    # Optional — PublicApiKeyFilter lets anonymous callers through; a key just makes this
    # bot's traffic identifiable/revocable. Unset is fine.
    hms_api_key: str | None = None

    # This bot's OWN state (conversation_state/processed_messages/pending_appointments) —
    # a separate database on the same SQL Server instance easyHMSAPI already runs on this VM,
    # never the HMS database itself.
    sqlserver_conn_string: str


settings = Settings()

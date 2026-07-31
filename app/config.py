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

    # NOT the standalone NLP service's own port 5003 — in this dev environment the reachable
    # entry point is nexeagle-website-dev's own /api/search/parse proxy route (port 82),
    # reached via host.docker.internal the same way SQLSERVER_CONN_STRING is. See
    # app/symptom_client.py for the specialtyId-slug -> internal-label translation this
    # requires that calling the model directly wouldn't have needed.
    symptom_api_base_url: str = "http://host.docker.internal:82"

    # City index (app/city_index.py) — maps a patient's shared GPS onto a city name that
    # /public/doctors?city= will match, built from 1HMS's own doctor data rather than a
    # hard-coded town list. Rebuilt at most once a day; the whole directory is paged through
    # on a cache miss, so the page size is deliberately large and the page cap is a guard
    # against an unexpectedly huge directory, not an expected limit.
    city_index_ttl_seconds: int = 86400
    city_index_page_size: int = 200
    city_index_max_pages: int = 60

    # The clinics' own timezone. Pinned rather than relying on the container's clock: the
    # Docker image sets no TZ, so date.today() there is the UTC date, and IST runs 5:30
    # ahead. Between midnight and 05:30 IST the UTC date is still *yesterday*, so a patient
    # tapping "Today" at 1am would have been booked into a date that had already passed —
    # silently, since a past date is still a valid date as far as the booking API is
    # concerned. Everything patient-facing about days and times must use this zone.
    clinic_timezone: str = "Asia/Kolkata"

    # Doctor search radius bands, in km. Look close first, widen only if that comes up
    # empty, and never go past the last band on the patient's behalf — beyond this we ask
    # before showing anything, because a patient in a Tier 2/3 town has no use for a doctor
    # several hundred km away unless they've said they're willing to travel.
    #
    # Distance is measured from the patient's shared coordinates to each doctor's own
    # coordinates, NOT by city name. That matters: the same town name can exist in more than
    # one place, and 1HMS's data currently has records labelled "Kishanganj" sitting at Delhi
    # coordinates. Ranking on real distance means those simply fall outside the band instead
    # of needing a special case.
    doctor_search_radii_km: list[float] = [10.0, 75.0]
    # How many index cities to pull doctors from when covering a radius. A guard against a
    # dense metro producing dozens of API calls; nearest cities are used first.
    doctor_search_max_cities: int = 8

    # Shared secret for the /events/* push endpoints (app/webhook.py) that 1HMS/1Rad will
    # call to push queue/token updates, prescription-ready, report-ready events into this
    # gateway. NOT the same secret as WHATSAPP_APP_SECRET — that one authenticates Meta,
    # this one authenticates the 1HMS platform itself, which is a different caller with a
    # different trust boundary. Required (no default) so a misconfigured deploy fails loud
    # rather than silently accepting unauthenticated event pushes.
    internal_events_token: str



settings = Settings()

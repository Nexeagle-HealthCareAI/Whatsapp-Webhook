from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    whatsapp_app_secret: str
    whatsapp_flow_id: str | None = "1534474454316152"
    whatsapp_flow_screen_id: str = "REGISTRATION_SCREEN"
    # The dialable number used in wa.me/<number> click-to-chat links -- NOT the same value as
    # whatsapp_phone_number_id above, which is Meta's internal id for the Cloud API, never
    # dialable. Used by GET /c/{hospital_code} (app/webhook.py) to build the redirect that
    # opens WhatsApp with "CHECKIN <code>" prefilled. Optional (unlike internal_events_token)
    # deliberately: this is a new addition to an already-deployed service, and every other
    # required setting here is read from a .env the deploy workflow generates from GitHub
    # secrets — an unset required field would crash-loop the whole bot on the next deploy
    # until that secret exists. GET /c/{code} degrades to a friendly error instead.
    whatsapp_display_number: str | None = None

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

    # Location-search API (app/messengers/location_client.py) — takes a typed place name and
    # returns up to `limit` real, canonical matches (city/district/town, state, coordinates)
    # so a typed city can be disambiguated the same way an ambiguous doctor/hospital name
    # already is, instead of the old single-guess-or-nothing local match against 1HMS's own
    # (much smaller) set of cities that happen to already have a doctor registered. Dev
    # subdomain for now — the prod one (loc.nexeagle.com) is currently down (TLS handshake
    # failing server-side); swap this once that's fixed, no code change needed either way.
    location_api_base_url: str = "https://loc-dev.nexeagle.com"

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

    # Doctor search radius bands, in km — progressively wider, nearest band first, only
    # trying the next if the current one comes up empty. Past the last band (50km), the
    # search auto-widens to unrestricted rather than asking the patient first — a product
    # decision (see the design flowchart's "auto-widen" branch): a patient in a Tier 2/3
    # town would rather see a doctor several hundred km away than a dead end.
    #
    # Distance is measured from the patient's shared coordinates to each doctor's own
    # coordinates, NOT by city name. That matters: the same town name can exist in more than
    # one place, and 1HMS's data currently has records labelled "Kishanganj" sitting at Delhi
    # coordinates. Ranking on real distance means those simply fall outside the band instead
    # of needing a special case.
    doctor_search_radii_km: list[float] = [10.0, 25.0, 50.0]
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
    sarvam_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    nlu_confidence_threshold: float = 0.75

    # Outbound WhatsApp send pacing (app/messengers/outbound_queue.py, sender.py). Meta's
    # own Cloud API throughput ceiling is commonly cited around 80 messages/sec per business
    # phone number — sending faster than that gets some messages rate-limited (HTTP 429).
    # Kept a little under that documented figure on purpose, not right at the edge: this
    # number is shared across every conversation happening at once, and other traffic to the
    # same number (template sends, 1HMS event pushes) isn't accounted for here.
    whatsapp_send_rate_limit: int = 70
    whatsapp_send_max_attempts: int = 5


settings = Settings()

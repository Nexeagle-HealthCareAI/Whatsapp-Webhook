"""
app/messengers/
----------------
Layer 7 -- the only code allowed to talk to another system. One file per external system:
hms_client.py (1HMS), whatsapp_client.py (Meta Graph API), redis_client.py (Redis),
symptom_client.py (NexEagleWebsite's symptom-routing proxy), city_index.py (built by
paging hms_client's own doctor directory). app/db/ is Messenger-layer too, kept as its own
top-level package rather than nested here since it already has 6 files of its own. See
docs/architecture-components.md.
"""

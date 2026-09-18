"""
app/pii.py
-----------
One shared helper for masking a patient's phone number before it goes into a log line --
logs are read by anyone with repo/Actions access, not just the person debugging a specific
incident, so a full number has no business appearing there. Same "keep the last 4 digits"
convention 1HMS's own WhatsAppMessagingService.MaskMobile uses, so a masked number looks the
same across both systems' logs.

Never use this to redact anything actually stored (DB rows, the outbound message itself) --
only for what a logger.* call prints.
"""


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "****"
    if len(phone) <= 4:
        return "*" * len(phone)
    return "*" * (len(phone) - 4) + phone[-4:]

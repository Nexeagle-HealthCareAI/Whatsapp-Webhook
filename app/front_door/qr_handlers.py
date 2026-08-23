import logging
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote

from fastapi import Response
from fastapi.responses import RedirectResponse

from app.messengers import hms_client
from app.config import settings
from app.messengers.hms_client import HmsApiError

logger = logging.getLogger("webhook.qr_handlers")


class BaseRedirectHandler(ABC):
    @property
    @abstractmethod
    def display_name(self) -> str:
        """Name of the feature, used in logging/error message."""
        pass

    @property
    def check_display_number(self) -> bool:
        """Whether to check if settings.whatsapp_display_number is configured."""
        return True

    @abstractmethod
    async def validate_resource(self, resource_id: str) -> Any:
        """Perform backend resource validation (raises HmsApiError if invalid) and return
        whatever was resolved (a hospital/doctor dict, a URL, ...) -- handed to
        build_wa_payload below so a subclass can put a human-readable name in the pre-filled
        WhatsApp text instead of just the bare code, without a second lookup."""
        pass

    @abstractmethod
    def build_wa_payload(self, resource_id: str, resolved: Any) -> str:
        """Return the pre-filled WhatsApp text body payload."""
        pass

    async def handle_redirect(self, resource_id: str) -> Response:
        if self.check_display_number and not settings.whatsapp_display_number:
            logger.warning(
                "GET redirect hit for %s but WHATSAPP_DISPLAY_NUMBER isn't configured yet",
                self.display_name,
            )
            return Response(
                content=f"{self.display_name} isn't set up yet. Please ask reception for help.",
                media_type="text/plain",
                status_code=503,
            )

        try:
            resolved = await self.validate_resource(resource_id)
        except HmsApiError:
            logger.warning("Resource validation failed for %s ID: %s", self.display_name, resource_id)
            return Response(
                content="This QR code isn't valid or has expired.",
                media_type="text/plain",
                status_code=404,
            )

        wa_text = quote(self.build_wa_payload(resource_id, resolved))
        return RedirectResponse(
            f"https://wa.me/{settings.whatsapp_display_number}?text={wa_text}"
        )


class CheckinRedirectHandler(BaseRedirectHandler):
    @property
    def display_name(self) -> str:
        return "Check-in"

    async def validate_resource(self, resource_id: str) -> dict:
        hospital = await hms_client.get_hospital_by_code(resource_id)
        logger.info(
            "QR scan for hospital %s (%s)",
            hospital.get("hospitalId"),
            hospital.get("name"),
        )
        return hospital

    def build_wa_payload(self, resource_id: str, resolved: dict) -> str:
        return f"CHECKIN {resource_id}"


class DoctorBookingRedirectHandler(BaseRedirectHandler):
    @property
    def display_name(self) -> str:
        return "Doctor booking"

    async def validate_resource(self, resource_id: str) -> dict:
        return await hms_client.get_doctor_by_id(resource_id)

    def build_wa_payload(self, resource_id: str, resolved: dict) -> str:
        return f"DRBOOK {resource_id}"


class HospitalBookingRedirectHandler(BaseRedirectHandler):
    @property
    def display_name(self) -> str:
        return "Hospital booking"

    async def validate_resource(self, resource_id: str) -> dict:
        return await hms_client.get_hospital_by_code(resource_id)

    def build_wa_payload(self, resource_id: str, resolved: dict) -> str:
        # Human-readable, not just the bare machine code -- the patient sees this in their
        # own sent-message bubble before the bot ever replies, so it names the hospital they
        # actually scanned. "QR ID of this hospital is {resource_id}" stays as a trailing,
        # still-deterministically-matchable phrase so _HOSPITAL_BOOKING_TRIGGER_PATTERN
        # (app/conversation/checkin.py) keeps matching it via search() instead of match().
        hospital_name = resolved.get("name") or "this hospital"
        return f"Hey, I've scanned the QR code for {hospital_name}! QR ID of this hospital is {resource_id}"


class DocumentRedirectHandler(BaseRedirectHandler):
    def __init__(self, code_prefix: str, resolver):
        self.code_prefix = code_prefix
        self.resolver = resolver

    @property
    def display_name(self) -> str:
        return self.code_prefix

    async def validate_resource(self, resource_id: str) -> Any:
        return await self.resolver(resource_id)

    def build_wa_payload(self, resource_id: str, resolved: Any) -> str:
        return f"{self.code_prefix} {resource_id}"

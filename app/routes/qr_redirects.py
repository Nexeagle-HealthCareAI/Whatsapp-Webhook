from fastapi import APIRouter, Response
from fastapi.responses import RedirectResponse

from app import hms_client
from app.config import settings
from app.routes.qr_handlers import (
    CheckinRedirectHandler,
    DoctorBookingRedirectHandler,
    DocumentRedirectHandler,
)

router = APIRouter()


@router.get("/c/{hospital_code}")
async def checkin_qr_redirect(hospital_code: str):
    return await CheckinRedirectHandler().handle_redirect(hospital_code)


@router.get("/d/{access_token}")
async def discharge_qr_redirect(access_token: str):
    handler = DocumentRedirectHandler(
        "DISCHARGE", hms_client.get_discharge_summary_url
    )
    return await handler.handle_redirect(access_token)


@router.get("/rx/{attachment_id}")
async def prescription_qr_redirect(attachment_id: str):
    handler = DocumentRedirectHandler(
        "RX", hms_client.get_prescription_attachment_url
    )
    return await handler.handle_redirect(attachment_id)


@router.get("/rxv/{appointment_id}")
async def visit_summary_qr_redirect(appointment_id: str):
    handler = DocumentRedirectHandler("RXV", hms_client.get_visit_summary_url)
    return await handler.handle_redirect(appointment_id)


@router.get("/doc/{doctor_id}")
async def doctor_booking_qr_redirect(doctor_id: str):
    return await DoctorBookingRedirectHandler().handle_redirect(doctor_id)


@router.get("/start")
async def generic_whatsapp_redirect():
    if not settings.whatsapp_display_number:
        return Response(
            content="WhatsApp chat isn't set up yet. Please call the hospital directly.",
            media_type="text/plain",
            status_code=503,
        )
    return RedirectResponse(f"https://wa.me/{settings.whatsapp_display_number}")

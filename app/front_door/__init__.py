from fastapi import APIRouter

from app.front_door.hms_events import router as events_router
from app.front_door.qr_redirects import router as redirects_router
from app.front_door.whatsapp_ingest import router as ingest_router

router = APIRouter()
router.include_router(ingest_router)
router.include_router(redirects_router)
router.include_router(events_router)

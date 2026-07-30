import logging

from fastapi import FastAPI

from app.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="WhatsApp Appointment Booking")
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"status": "ok"}

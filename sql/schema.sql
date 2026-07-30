-- Run once against the SQL Server instance already on the Dev VM (port 1433) — creates a
-- separate database for THIS bot's own state, never the easyHMSAPI database itself.
IF DB_ID('WhatsAppBookingDev') IS NULL
BEGIN
    CREATE DATABASE WhatsAppBookingDev;
END
GO

USE WhatsAppBookingDev;
GO

IF OBJECT_ID('dbo.conversation_state') IS NULL
BEGIN
    CREATE TABLE dbo.conversation_state (
        phone_number NVARCHAR(20) NOT NULL PRIMARY KEY,
        current_step NVARCHAR(50) NOT NULL,
        context_json NVARCHAR(MAX) NULL,
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- Durable backstop beyond Redis's TTL-based dedupe (app/webhook.py) — belt and suspenders
-- against a duplicate booking if a job is ever replayed after the Redis key has expired.
IF OBJECT_ID('dbo.processed_messages') IS NULL
BEGIN
    CREATE TABLE dbo.processed_messages (
        message_id NVARCHAR(100) NOT NULL PRIMARY KEY,
        processed_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- Local mirror of what this bot has asked easyHMSAPI's public API to book. Needed because
-- the public API has no phone-based lookup for guest bookings, so idempotency (don't
-- double-submit the same request on retry) has to be checked here, not against the HMS DB.
IF OBJECT_ID('dbo.pending_appointments') IS NULL
BEGIN
    CREATE TABLE dbo.pending_appointments (
        id UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
        phone_number NVARCHAR(20) NOT NULL,
        preferred_date DATE NOT NULL,
        hms_appointment_id UNIQUEIDENTIFIER NULL,
        status NVARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_pending_appointments_phone_date
        ON dbo.pending_appointments(phone_number, preferred_date, status);
END
GO

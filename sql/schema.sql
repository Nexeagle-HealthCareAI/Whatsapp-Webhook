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

-- Added for: language selection, family/proxy booking, and follow-up notifications.
-- Guarded with COL_LENGTH checks (not IF OBJECT_ID, which is for whole objects) so this
-- script stays safe to re-run against a database that already has pending_appointments
-- from before these columns existed.
IF COL_LENGTH('dbo.pending_appointments', 'preferred_language') IS NULL
BEGIN
    -- 'en' / 'hi' / 'hg' — see app/i18n.py. Stored per-booking (not just in the ephemeral
    -- Redis/conversation_state context) because the follow-up scheduler reads it long after
    -- the conversation itself has ended and conversation_state has been cleared.
    ALTER TABLE dbo.pending_appointments ADD preferred_language NVARCHAR(10) NULL;
END
GO

IF COL_LENGTH('dbo.pending_appointments', 'booking_for') IS NULL
BEGIN
    ALTER TABLE dbo.pending_appointments ADD booking_for NVARCHAR(20) NOT NULL DEFAULT 'self';
END
GO

IF COL_LENGTH('dbo.pending_appointments', 'patient_display_name') IS NULL
BEGIN
    -- The name used ON the appointment (may be a family member's name, not the WhatsApp
    -- account holder's) — kept separately from phone_number, which stays the contact
    -- number confirmations/queue updates are sent to regardless of who the patient is.
    ALTER TABLE dbo.pending_appointments ADD patient_display_name NVARCHAR(200) NULL;
END
GO

IF COL_LENGTH('dbo.pending_appointments', 'followup_sent_at') IS NULL
BEGIN
    ALTER TABLE dbo.pending_appointments ADD followup_sent_at DATETIME2 NULL;
END
GO

-- Latest known queue/token position per HMS appointment, updated by POST /events/token-called
-- (app/webhook.py) whenever 1HMS pushes one. Read back by db.get_appointment_by_hms_id() to
-- decide who to notify and in what language.
IF OBJECT_ID('dbo.appointment_queue_status') IS NULL
BEGIN
    -- Same type as pending_appointments.hms_appointment_id (UNIQUEIDENTIFIER) — both
    -- ultimately hold whatever easyHMSAPI's POST /public/appointments returned as
    -- appointmentId, so they need to match for the join in db.get_appointment_by_hms_id().
    CREATE TABLE dbo.appointment_queue_status (
        hms_appointment_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        current_token INT NOT NULL,
        estimated_wait_minutes INT NULL,
        updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

IF COL_LENGTH('dbo.pending_appointments', 'patient_age') IS NULL
BEGIN
    ALTER TABLE dbo.pending_appointments ADD patient_age INT NULL;
END
GO

IF COL_LENGTH('dbo.pending_appointments', 'patient_gender') IS NULL
BEGIN
    ALTER TABLE dbo.pending_appointments ADD patient_gender NVARCHAR(10) NULL;
END
GO

IF COL_LENGTH('dbo.pending_appointments', 'patient_guardian') IS NULL
BEGIN
    ALTER TABLE dbo.pending_appointments ADD patient_guardian NVARCHAR(200) NULL;
END
GO


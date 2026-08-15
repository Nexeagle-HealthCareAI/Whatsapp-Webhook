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

-- One row per conversation session (session_id from app/conversation/language.py's _start,
-- carried through context for the session's whole lifetime) -- NOT one row per message.
-- transcript_json accumulates every inbound message (full content) and every step the bot
-- moved the patient to, via JSON_MODIFY 'append $' from conversation_logger.py -- that
-- process is the only writer, so the read-modify-write JSON append never races against
-- itself. appointment_id stays NULL until the booking actually completes; a session with no
-- appointment_id is, by definition, one the patient started and did not finish -- no separate
-- "abandoned" flag needed, and no risk of it going stale/desyncing from the real outcome.
--
-- Deliberately populated OFF the request path: app/messengers/conversation_log_queue.py just
-- LPUSHes a small job (sub-millisecond) from the inbound-message and step-transition hooks;
-- conversation_logger.py (its own process, same convention as worker.py/sender.py) is what
-- actually writes here. A patient's reply is never slowed down by a SQL Server round-trip.
IF OBJECT_ID('dbo.conversation_sessions') IS NULL
BEGIN
    CREATE TABLE dbo.conversation_sessions (
        session_id NVARCHAR(100) NOT NULL PRIMARY KEY,
        phone_number NVARCHAR(20) NOT NULL,
        started_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_activity_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        last_step NVARCHAR(50) NULL,
        appointment_id UNIQUEIDENTIFIER NULL,
        transcript_json NVARCHAR(MAX) NULL
    );
    CREATE INDEX IX_conversation_sessions_phone
        ON dbo.conversation_sessions(phone_number, last_activity_at DESC);
END
GO

-- Logger table for tracking NLU brain requests, parses, and accuracy metrics
IF OBJECT_ID('dbo.nlu_logs') IS NULL
BEGIN
    CREATE TABLE dbo.nlu_logs (
        id INT IDENTITY(1,1) PRIMARY KEY,
        phone_number NVARCHAR(20) NOT NULL,
        session_id NVARCHAR(100) NULL,
        utterance NVARCHAR(MAX) NOT NULL,
        nlu_brain NVARCHAR(50) NOT NULL,
        detected_intent NVARCHAR(100) NULL,
        confidence FLOAT NULL,
        doctor_name NVARCHAR(200) NULL,
        specialty NVARCHAR(200) NULL,
        symptom NVARCHAR(MAX) NULL,
        formatted_date NVARCHAR(20) NULL,
        routed_step NVARCHAR(50) NULL,
        is_correct BIT NULL,
        user_feedback NVARCHAR(100) NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_nlu_logs_phone
        ON dbo.nlu_logs(phone_number, created_at DESC);
    CREATE INDEX IX_nlu_logs_session
        ON dbo.nlu_logs(session_id);
END
GO



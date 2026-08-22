# API requirement: update doctor / update patient details on an existing appointment

## Why this is needed

The WhatsApp bot's "check my appointment" flow offers a patient three follow-up actions once
they've confirmed an appointment exists: **Cancel**, **Update**, **Book another**. Cancel and
Book-another are already fully supported by the existing public API
(`PATCH /public/appointments/{id}/cancel`, `POST /public/appointments`). **Update is not** —
today the only "update" 1HMS's public API exposes is `PATCH /public/appointments/{id}/reschedule`,
which changes the appointment's **date only**. There is currently no way to change which
**doctor** an appointment is with, or correct the **patient's details** (name/age/gender/
guardian) on an appointment that's already booked, without cancelling it and creating a new one
from scratch — which loses the original booking's queue position and requires the patient to
re-enter everything.

This document specifies the two new endpoints needed to support that properly. Both follow the
exact conventions of the three endpoints already in production (`cancel`, `reschedule`,
`GET /public/appointments/{id}`) so they slot into the same `PublicController`/handler family
without introducing a new pattern.

## Existing conventions these should match

- Base path: `/public/appointments/{appointmentId}/...`
- Method: `PATCH` (an existing appointment is being modified, not created)
- Auth: no bearer/API-key requirement beyond what `PublicApiKeyFilter` already allows through —
  the **request body's `mobile` field is cross-checked server-side against the appointment's own
  patient record**, same as `cancel`/`reschedule`. A mismatch should return the same generic
  "not found" style response those two already use (see below), not a distinct error, so a
  guesser can't use response differences to enumerate valid appointment IDs.
- Response shape: `{"success": bool, "message": string, ...}` — the caller relays `message`
  straight to the patient on both success and failure, so it needs to already be a complete,
  patient-facing sentence (matching how `cancel`/`reschedule`'s messages are used today).
- Timeout expectation on the caller side: 15s (same as `cancel`/`reschedule`/`book`).

## Endpoint 1: Update doctor

```
PATCH /public/appointments/{appointmentId}/update-doctor
```

**Request body**

```json
{
  "mobile": "919876543210",
  "newDoctorId": "d-123"
}
```

**Response**

```json
{
  "success": true,
  "message": "Your appointment has been moved to Dr. Neha Sen.",
  "appointment": {
    "appointmentId": "appt-1",
    "doctorName": "Dr. Neha Sen",
    "apptDate": "2026-08-22T00:00:00",
    "statusCode": "FUTURE"
  }
}
```

(`appointment` echoes the same shape `GET /public/appointments/{id}` already returns, so the
caller can update its own cached copy without a second lookup.)

**Open questions for the backend team to decide** (the bot will adapt to whatever the answer is,
but needs to know before it can build the confirm-prompt copy correctly):

1. **Does the appointment keep its original date, or does it need the new doctor's availability
   re-checked for that date?** If the new doctor doesn't work that day/shift, what should happen —
   reject with a clear message (preferred, so the bot can tell the patient exactly why), or
   silently move to the doctor's next available date?
2. **Does the consultation fee change** if the new doctor's fee differs from the original? Should
   the response include the new fee so the bot can tell the patient before they confirm?
3. **Can the new doctor be at a different hospital** than the original, or must they be at the
   same hospital? If a hospital change is allowed, does `hospitalId` need to move too, and should
   that be echoed back in the response?
4. Same queue-position caveat as reschedule: does changing doctor put the appointment at the back
   of that doctor's queue, or preserve original booking order? (Only matters if queue position is
   patient-visible elsewhere, e.g. token numbers.)

## Endpoint 2: Update patient details

```
PATCH /public/appointments/{appointmentId}/update-patient
```

**Request body** (all patient fields optional except `mobile` — send only what changed; this
matches `book_appointment`'s existing `patient: {fullName, mobile}` shape rather than inventing a
new one)

```json
{
  "mobile": "919876543210",
  "patient": {
    "fullName": "Aquib Khan",
    "age": 58,
    "gender": "male",
    "guardian": "Rajesh Khan"
  }
}
```

**Response**

```json
{
  "success": true,
  "message": "Patient details updated.",
  "appointment": {
    "appointmentId": "appt-1",
    "doctorName": "Dr. Neha Sen",
    "apptDate": "2026-08-22T00:00:00",
    "statusCode": "FUTURE"
  }
}
```

**Open questions for the backend team:**

1. Does `PublicBookAppointmentRequestModel`'s `patient` object already have `age`/`gender`/
   `guardian` fields, or only `fullName`/`mobile` (the bot currently folds age/gender/guardian
   into the free-text `reason` field on create, per `book_appointment`'s own code — see its
   comment on `extra_note`)? If those fields don't exist on the appointment record at all today,
   this endpoint needs them added there first, not just on this new PATCH.
2. Can `mobile` itself be corrected via this endpoint (e.g. patient gives a different contact
   number), or does changing the contact number require a different flow entirely (since `mobile`
   is also the field this and every other endpoint cross-checks auth against)? Recommend: **not
   supported here** — treat a mobile-number correction as a cancel + rebook, to avoid the
   auth-cross-check field being mutable in the same request that uses it for auth.

## What the bot will do once these exist

`app/messengers/hms_client.py` gets two new thin wrapper functions
(`update_appointment_doctor(appointment_id, mobile, new_doctor_id)` and
`update_appointment_patient(appointment_id, mobile, patient_details)`), matching
`cancel_appointment`/`reschedule_appointment`'s existing style exactly (return the raw
`{success, message}` dict, don't raise on `success: false`, `@_retry_network_errors` retry
wrapper, 15s timeout). `app/conversation/appointment_actions.py`'s "Update Doctor" / "Update
Patient Details" buttons (see `_handle_viewing_appointment_status`) call into these directly,
confirmed via the same Yes/No button pattern every other real action already uses
(`_send_appointment_confirm_prompt`) — no cancel-and-rebook workaround needed once this exists.

from typing import Any, Optional, TypedDict

class ConversationContext(TypedDict, total=False):
    """
    Interface Segregation Principle (ISP) compliant session state schema.
    Consolidates all permitted keys in the conversation session context,
    providing developers and tools with a strict schema contract.
    """
    lang: str
    guess_lang: Optional[str]
    patient_lat: Optional[float]
    patient_lng: Optional[float]
    location_text: Optional[str]
    city: Optional[str]
    city_distance_km: Optional[float]
    booking: dict[str, Any]
    doctor_options: dict[str, Any]
    doctor_fee: Optional[float]
    doctor_name: Optional[str]
    date_label: Optional[str]
    shift_label: Optional[str]
    appt_action_options: dict[str, Any]
    appt_action: Optional[str]
    appt_action_detail: dict[str, Any]
    appt_action_new_date: Optional[str]
    appt_action_id: Optional[str]
    checkin_hospital_name: Optional[str]
    checkin_options: dict[str, Any]
    specialty_groups: dict[str, Any]
    specialty_category: Optional[str]
    search_doctor_query: Optional[str]
    _history: list[str]

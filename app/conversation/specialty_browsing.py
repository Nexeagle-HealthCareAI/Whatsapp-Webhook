"""
app/conversation/specialty_browsing.py
----------------------------------------
Symptom/specialty search & browsing domain: row/group formatting, the search-mode
prompt (symptom vs name vs browse), symptom-to-specialty routing, the two-level
specialty browse list, and the sort-options prompt.

Cross-references back into app/conversation/__init__.py (whatsapp_client and db,
two of the 9 names the test suite reassigns directly; _transition_to, _phrase,
_search_doctors_flow, _send_doctor_list, which are core-orchestration/other-domain
functions that still live there) are all made via a function-body-local
`from app import conversation` + `conversation.<name>(...)`, never a module-level
import -- see docs/architecture.md and app/conversation/checkin.py's module
docstring for why. Calls between functions that both live in THIS file
(_send_specialty_list, _send_sort_prompt, _handle_awaiting_symptom) stay as plain
same-module calls.
"""
import asyncio

from app import i18n, nlu_client
from app.messengers import hms_client, symptom_client
from app.i18n import t
from app.conversation.shared import _match_choice
from app.conversation.doctor_search import _is_doctor_search_query
from app.types import ConversationContext


async def resolve_specialty_category(client, query: str, categories: list[str]) -> str | None:
    """The one place every specialty/symptom-label match happens. Tries the fast, free,
    deterministic match first (symptom_client.match_category) -- covers correct spelling
    and shorthand ("gyno", "cardio") since those are substrings of the real category name.
    Only when that finds nothing (typically a misspelling like "kardio"/"cardeo") does it
    ask the Listener's AI to pick from the real category list -- see
    nlu_client.disambiguate_specialty for why that's safe (closed-set choice, not free
    generation). The AI's answer is re-validated here against `categories` before being
    trusted at all -- it can only ever return something that was already a real, valid
    option, never something invented.

    nlu_client is safe to call directly here (not via the lazy `conversation.` pattern) --
    only ever attribute-patched in tests (conversation.nlu_client.classify_message = ...),
    never reassigned wholesale, same as hms_client."""
    matched = symptom_client.match_category(query, categories)
    if matched:
        return matched

    ai_guess = await nlu_client.disambiguate_specialty(client, query, categories)
    if not ai_guess:
        return None
    for category in categories:
        if category.lower() == ai_guess.lower():
            return category
    return None


def _specialty_row(specialty: dict) -> tuple[str, str, str]:
    """(row_id, title, description) for one specialty.

    row_id must stay the raw category string — that's what comes back as list_reply.id and
    gets passed straight to /public/doctors?specialtyCategory=. Title picks whichever of the
    category-without-parenthetical or the API's displayName is shorter, because WhatsApp
    truncates row titles at 24 chars and "Endocrinologist (Hormones/Diabetes)" would become
    "Endocrinologist (Hormone". The full displayName goes in the description line, so the
    longer official wording is still visible either way."""
    category = specialty["category"]
    display = (specialty.get("displayName") or "").strip() or category
    base = category.split("(")[0].strip()
    title = min([base, display], key=len)
    # Still too long ("Sports Medicine Specialist" is 26) — drop the redundant trailing noun
    # rather than let WhatsApp hard-cut mid-word into "Sports Medicine Speciali".
    if len(title) > 24:
        for suffix in (" Specialist", " Surgeon", " Physician"):
            if title.endswith(suffix) and len(title) - len(suffix) >= 4:
                title = title[: -len(suffix)].strip()
                break
    return category, title, display


def _groups_with_live_categories(specialties: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Pairs each configured group with the live specialties actually in it, drops empty
    groups, and sweeps anything unrecognised into Other. Driven by the live API response
    rather than the static config, so a specialty 1HMS adds later still reaches a patient."""
    by_category = {s["category"]: s for s in specialties}
    claimed: set[str] = set()
    paired = []
    for group in i18n.SPECIALTY_GROUPS:
        members = [by_category[c] for c in group["categories"] if c in by_category]
        claimed.update(s["category"] for s in members)
        if members:
            paired.append((group, members))
    leftovers = [s for s in specialties if s["category"] not in claimed]
    if leftovers:
        paired.append((i18n.OTHER_GROUP, leftovers))
    return paired


async def _send_search_mode_prompt(client, phone: str, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    # Only the question is phrased — the three button labels stay templated, since they
    # have to match the ids _handle_choosing_search_mode matches against.
    body = await conversation._phrase(client, "choosing_search_mode", context, "search_mode_prompt")
    await conversation.whatsapp_client.send_buttons(
        client, phone, body,
        [
            ("symptom", t("search_mode_symptom", lang)),
            ("name", t("search_mode_name", lang)),
            ("browse", t("search_mode_browse", lang)),
        ],
    )
    await conversation._transition_to(phone, "choosing_search_mode", context, "choosing_location")


async def _handle_choosing_search_mode(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type == "text" and _is_doctor_search_query(input_value):
        context = {**context, "search_doctor_query": input_value}
        if await conversation._search_doctors_flow(client, phone, context, "choosing_search_mode"):
            return
        else:
            await conversation.whatsapp_client.send_text(
                client, phone, t("search_doctor_not_found", lang, query=input_value)
            )
            context.pop("search_doctor_query", None)

    choice = _match_choice(input_type, input_value, ["symptom", "name", "browse"])
    if choice is None:
        await conversation.whatsapp_client.send_text(client, phone, t("search_mode_choose_hint", lang))
        return
    if choice == "symptom":
        await conversation.whatsapp_client.send_text(
            client, phone, await conversation._phrase(client, "awaiting_symptom", context, "symptom_ask")
        )
        await conversation._transition_to(phone, "awaiting_symptom", context, "choosing_search_mode")
        return
    if choice == "name":
        await conversation.whatsapp_client.send_text(
            client, phone, await conversation._phrase(client, "awaiting_doctor_name", context, "doctor_name_ask")
        )
        await conversation._transition_to(phone, "awaiting_doctor_name", context, "choosing_search_mode")
        return
    await _send_specialty_list(client, phone, context)


async def _handle_awaiting_symptom(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type == "text" and _is_doctor_search_query(input_value):
        context = {**context, "search_doctor_query": input_value}
        if await conversation._search_doctors_flow(client, phone, context, "awaiting_symptom"):
            return
        else:
            await conversation.whatsapp_client.send_text(
                client, phone, t("search_doctor_not_found", lang, query=input_value)
            )
            context.pop("search_doctor_query", None)

    if input_type != "text" or not input_value.strip():
        await conversation.whatsapp_client.send_text(client, phone, t("symptom_text_required", lang))
        return

    # Independent fetches -- run concurrently instead of sequentially, same reasoning
    # as doctor_search.py's city_index calls.
    labels, specialties = await asyncio.gather(
        symptom_client.route_symptom(input_value), hms_client.list_specialties()
    )
    categories = [s["category"] for s in specialties]
    matched_category = None
    for label in labels:
        matched_category = await resolve_specialty_category(client, label, categories)
        if matched_category:
            break

    if not matched_category:
        await conversation.whatsapp_client.send_text(client, phone, t("symptom_no_match", lang))
        await _send_specialty_list(client, phone, context)
        return

    await _send_sort_prompt(
        client, phone, context, matched_category, "awaiting_symptom",
        concern_prefix=t("symptom_concern_only", lang, specialty=matched_category),
    )


async def _send_specialty_list(client, phone: str, context: ConversationContext) -> None:
    """First of two levels: the broad areas. See the comment above SPECIALTY_GROUPS in
    i18n.py for why browsing can't just list all 30 categories in one message."""
    from app import conversation

    lang = context.get("lang")
    specialties = await hms_client.list_specialties()
    if not specialties:
        await conversation.whatsapp_client.send_text(client, phone, t("no_specialties", lang))
        await conversation.db.clear_conversation_state(phone)
        return

    paired = _groups_with_live_categories(specialties)
    rows = []
    for group, members in paired:
        title, desc = i18n.group_label(group, lang)
        rows.append((group["id"], title, desc))

    await conversation.whatsapp_client.send_list(
        client, phone, t("specialty_group_prompt", lang), t("specialty_group_button", lang),
        rows, t("specialty_group_section", lang),
    )
    # Remember the group -> categories split that was actually shown, so the next step
    # doesn't have to re-fetch and risk showing a group built from a different response.
    group_members = {group["id"]: [s["category"] for s in members] for group, members in paired}
    await conversation._transition_to(
        phone, "choosing_specialty_group", {**context, "specialty_groups": group_members}, "choosing_search_mode"
    )


async def _handle_choosing_specialty_group(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    group_members = context.get("specialty_groups", {})
    if input_type != "list_reply" or input_value not in group_members:
        # A patient who types instead of tapping here is usually describing a symptom
        # ("ghutne mein dard") rather than fumbling the menu — so route them into symptom
        # search instead of scolding them for not tapping. Falls back to the hint only if
        # the NLP can't place it.
        if input_type == "text" and input_value.strip():
            await _handle_awaiting_symptom(client, phone, input_type, input_value, context)
            return
        await conversation.whatsapp_client.send_text(client, phone, t("specialty_group_choose_hint", lang))
        return

    categories = group_members[input_value]
    specialties = await hms_client.list_specialties()
    by_category = {s["category"]: s for s in specialties}
    members = [by_category[c] for c in categories if c in by_category]
    if not members:
        await conversation.whatsapp_client.send_text(client, phone, t("no_specialties", lang))
        await _send_specialty_list(client, phone, context)
        return

    # Single-specialty group — asking "which of these fits best?" for a list of one is the
    # kind of pointless tap that makes a bot feel bureaucratic. Skip straight to sorting.
    if len(members) == 1:
        await _send_sort_prompt(client, phone, context, members[0]["category"], "choosing_specialty_group")
        return

    rows = [_specialty_row(s) for s in members]
    await conversation.whatsapp_client.send_list(
        client, phone, t("specialty_list_prompt", lang), t("specialty_list_button", lang),
        rows, t("specialty_group_section", lang),
    )
    await conversation._transition_to(phone, "choosing_specialty", context, "choosing_specialty_group")


async def _handle_choosing_specialty(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type != "list_reply":
        await conversation.whatsapp_client.send_text(client, phone, t("specialty_choose_hint", lang))
        return
    await _send_sort_prompt(client, phone, context, input_value, "choosing_specialty")


async def _send_sort_prompt(
    client, phone: str, context: ConversationContext, specialty_category: str, current_step: str,
    concern_prefix: str | None = None,
) -> None:
    from app import conversation

    lang = context.get("lang")
    context = {**context, "specialty_category": specialty_category}
    rows = [
        ("rating", t("sort_rating", lang)),
        ("experience", t("sort_experience", lang)),
        ("fee", t("sort_fee", lang)),
    ]
    # "Nearest" only makes sense if we actually have something to measure distance from —
    # omitted rather than shown-and-broken when the patient only typed a city name with no
    # coordinates and that name doesn't help either (handled inside _sort_doctors, but no
    # point offering the option here if it can't do anything).
    if context.get("patient_lat") is not None or context.get("location_text"):
        rows.insert(1, ("nearest", t("sort_nearest", lang)))
    # concern_prefix names the specialty this search matched to (Task 4/5's concern/enthusiasm
    # framing) for callers where location was already known and the patient hasn't been told
    # yet what specialty their symptom/request resolved to -- folded into this one list message
    # rather than sent separately, so a matched specialty is never silently unannounced without
    # doubling up messages. Callers that already announced it (e.g. as part of an earlier
    # combined location-ask message) pass nothing.
    body = f"{concern_prefix}\n\n{t('sort_prompt', lang)}" if concern_prefix else t("sort_prompt", lang)
    await conversation.whatsapp_client.send_list(client, phone, body, t("sort_button", lang), rows, "Sort")
    await conversation._transition_to(phone, "choosing_sort", context, current_step)


async def _handle_choosing_sort(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, conversation._SORT_OPTIONS)
    if choice is None:
        await conversation.whatsapp_client.send_text(client, phone, t("sort_choose_hint", lang))
        return
    await conversation._send_doctor_list(client, phone, {**context, "sort_key": choice})

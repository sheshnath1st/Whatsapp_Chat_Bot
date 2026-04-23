import re
from datetime import datetime, timedelta

# ---
# Date/time resolution for WhatsApp → LLM → Salesforce
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def resolve_datetime(text: str) -> str:
    """
    Parse natural language date/time from text and return ISO 8601 datetime string (YYYY-MM-DDTHH:MM:SS).
    Returns None if no valid date found.
    """
    if not text or not isinstance(text, str):
        return None
    text = text.lower().strip()
    now = datetime.now()
    base_date = None

    # --- 1. Relative days ---
    if re.search(r"\btoday\b", text):
        base_date = now
    elif re.search(r"\btomorrow\b", text):
        base_date = now + timedelta(days=1)
    elif m := re.search(r"after (\d+) days", text):
        base_date = now + timedelta(days=int(m.group(1)))
    elif m := re.search(r"in (\d+) days", text):
        base_date = now + timedelta(days=int(m.group(1)))

    # --- 2. Weekday handling ---
    else:
        for wd in WEEKDAYS:
            # "next Monday" or just "Monday"
            if f"next {wd}" in text:
                today_idx = now.weekday()
                target_idx = WEEKDAYS.index(wd)
                days_ahead = (target_idx - today_idx + 7) % 7
                days_ahead = days_ahead or 7
                base_date = now + timedelta(days=days_ahead)
                break
            elif re.search(rf"\b{wd}\b", text):
                today_idx = now.weekday()
                target_idx = WEEKDAYS.index(wd)
                days_ahead = (target_idx - today_idx + 7) % 7
                # If today is the day, move to next week
                days_ahead = days_ahead or 7
                base_date = now + timedelta(days=days_ahead)
                break

    # --- 3. Time parsing ---
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    hour = 10
    minute = 0
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = time_match.group(3)
        if ampm:
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
    # If no date found, return None
    if not base_date:
        return None
    dt = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")
import os
import re
import asyncio
import tempfile
import json
import requests
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from typing import Optional
from conversation_store import (
    log_event,
    log_failure,
    store_pending_sf_context,
    get_pending_sf_context,
    clear_pending_sf_context,
    save_contact,
    update_contact_event,
    upsert_conversation_message,
    update_conversation_sf_and_context,
    get_recent_messages,
)

load_dotenv()

# Prefer ACCESS_TOKEN (used in README); fall back to META_ACCESS_TOKEN for backward compatibility.
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")
MEDIA_URL = "https://graph.facebook.com/v19.0/{media_id}"
BASE_URL = os.getenv("BASE_URL")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
AGENT_URL = os.getenv("AGENT_URL", "http://127.0.0.1:5000")
SALESFORCE_API_URL = os.getenv("SALESFORCE_API_URL")
SALESFORCE_COOKIE = os.getenv("SALESFORCE_COOKIE")
JSON_CONTENT_TYPE = "application/json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_history_for_llm(messages: list) -> str:
    """Convert recent conversation messages into a compact text block for LLM context."""
    if not messages:
        return ""
    parts = []
    for msg in messages:
        ts = msg.get("timestamp", "")
        msg_type = msg.get("type", "unknown")
        if msg_type == "image":
            extracted = msg.get("extracted") or {}
            name = extracted.get("name") or "unknown"
            company = extracted.get("company") or ""
            sf = (msg.get("salesforce") or {}).get("response") or {}
            sf_id = sf.get("sf_id") or ""
            line = f"[{ts}] IMAGE: Business card scanned — Name: {name}, Company: {company}"
            if sf_id:
                line += f", SF ID: {sf_id}"
            parts.append(line)
        elif msg_type == "audio":
            proc = msg.get("processing") or {}
            transcript = proc.get("transcript") or ""
            event = msg.get("event") or {}
            event_type = event.get("event_type") or event.get("type") or ""
            line = f"[{ts}] AUDIO: \"{transcript}\""
            if event_type and event_type.lower() != "none":
                due = event.get("due_date") or event.get("date") or ""
                line += f" → Event: {event_type}" + (f" on {due}" if due else "")
            parts.append(line)
        elif msg_type == "text":
            direction = msg.get("direction") or "user"
            content = msg.get("content") or ""
            parts.append(f"[{ts}] {direction.upper()}: {content}")
    return "\n".join(parts)


def _build_image_message_doc(
    *,
    message_id: Optional[str],
    media_id: Optional[str],
    extracted_data: dict,
    salesforce_payload: Optional[dict],
    salesforce_result: Optional[dict],
) -> dict:
    contact = (extracted_data.get("contact") if isinstance(extracted_data.get("contact"), dict) else {}) or {}
    meta = (extracted_data.get("metadata") if isinstance(extracted_data.get("metadata"), dict) else {}) or {}
    sf_id = (salesforce_result or {}).get("salesforce_id")
    sf_status = "created" if (salesforce_result or {}).get("ok") else "failed"
    return {
        "message_id": message_id,
        "type": "image",
        "timestamp": _utc_now_iso(),
        "media": {"id": media_id, "url": None, "stored_url": None, "mime_type": "image/jpeg"},
        "processing": {
            "ocr_text": None,
            "confidence": meta.get("confidence_score"),
        },
        "extracted": {
            "name": contact.get("full_name") or contact.get("name"),
            "phone": contact.get("phone"),
            "email": contact.get("email"),
            "company": contact.get("company"),
            "designation": contact.get("designation"),
        },
        "llm": {"prompt_version": "v1", "raw_output": {}},
        "salesforce": {
            "payload": salesforce_payload,
            "response": {"sf_id": sf_id, "status": sf_status},
        },
    }


def _build_audio_message_doc(
    *,
    message_id: Optional[str],
    media_id: Optional[str],
    transcript: str,
    event: Optional[dict],
    salesforce_payload: Optional[dict],
    salesforce_result: Optional[dict],
    reply_to: Optional[str] = None,
) -> dict:
    sf_status = "updated" if (salesforce_result or {}).get("ok") else "failed"
    return {
        "message_id": message_id,
        "type": "audio",
        "timestamp": _utc_now_iso(),
        "reply_to": reply_to,
        "media": {"id": media_id, "stored_url": None, "mime_type": "audio/ogg"},
        "processing": {"transcript": transcript, "language": "en", "confidence": None},
        "event": event,
        "salesforce": {
            "payload": salesforce_payload,
            "response": {"status": sf_status},
        },
    }


def _extract_whatsapp_message_id(response_json: dict) -> Optional[str]:
    if not isinstance(response_json, dict):
        return None
    messages = response_json.get("messages") if isinstance(response_json.get("messages"), list) else []
    if not messages:
        return None
    return messages[0].get("id")


def _send_message_once(payload: dict, headers: dict) -> dict:
    response = requests.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=15)
    response_json = {}
    try:
        response_json = response.json()
    except Exception:
        pass
    return {
        "status_code": response.status_code,
        "response_json": response_json,
        "response_text": response.text,
        "message_id": _extract_whatsapp_message_id(response_json),
    }

def send_message(to: str, text: str):
    print(f"Preparing to send message to {to}: {text}")
    if not text:
        print("Error: Message text is empty.")
        log_failure(
            source="send_message.empty_text",
            error="empty_text",
            phone=to,
            payload={"text": text},
        )
        return {"ok": False, "error": "empty_text"}

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": JSON_CONTENT_TYPE
    }
    print(f"Send URL WHATSAPP_API_URL: {WHATSAPP_API_URL}")
    print(f"Send URL headers: {headers}")
    print(f"Send URL payload: {payload}")

    for attempt in range(3):
        try:
            send_attempt = _send_message_once(payload, headers)
            if send_attempt["status_code"] == 200:
                print("Message sent")
                return {
                    "ok": True,
                    "status_code": send_attempt["status_code"],
                    "message_id": send_attempt["message_id"],
                    "response": send_attempt["response_json"],
                }
            print(f"Send failed (attempt {attempt + 1}): {send_attempt['response_text']}")
        except requests.exceptions.Timeout:
            print(f"Send timeout (attempt {attempt + 1})")
        except Exception as exc:
            print(f"Send error (attempt {attempt + 1}): {exc}")
            break  # Non-transient error; don't retry

    return {"ok": False, "error": "whatsapp_send_failed"}



async def send_message_async(user_phone: str, message: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_message, user_phone, message)



        
def send_to_salesforce(business_card_data: dict):
    """Send extracted business card data to Salesforce API."""
    if not SALESFORCE_API_URL:
        print("Warning: SALESFORCE_API_URL not configured, skipping Salesforce sync")
        log_failure(
            source="send_to_salesforce.not_configured",
            error="salesforce_url_not_configured",
            payload={"business_card_data": business_card_data},
        )
        return {"ok": False, "error": "salesforce_url_not_configured"}
    
    try:
        headers = {
            "Content-Type": JSON_CONTENT_TYPE
        }
        if SALESFORCE_COOKIE:
            headers["Cookie"] = SALESFORCE_COOKIE
        
        print(f"Sending business card data to Salesforce: {business_card_data}")
        response = requests.post(
            SALESFORCE_API_URL,
            headers=headers,
            json=business_card_data,
            timeout=15
        )

        response_body = None
        try:
            response_body = response.json()
        except Exception:
            response_body = response.text

        salesforce_id = None
        print(f"Salesforce response body: {response_body}")
        if isinstance(response_body, dict):
            salesforce_id = (
                response_body.get("id")
                or response_body.get("Id")
                or response_body.get("recordId")
                or response_body.get("salesforce_id")
                or response_body.get("sf_id")
            )
        # Fallback: extract Salesforce ID from plain-string responses
        # e.g. "Lead created successfully with Id: 00Qa3001FA0Mt64EYC"
        if not salesforce_id and isinstance(response_body, str):
            match = re.search(r'\b([A-Za-z0-9]{15,18})\b', response_body)
            if match:
                salesforce_id = match.group(1)
        
        if 200 <= response.status_code < 300:
            print("Business card data sent to Salesforce successfully")
            return {
                "ok": True,
                "status_code": response.status_code,
                "salesforce_id": salesforce_id,
                "response": response_body,
            }
        else:
            print(f"Salesforce API error (status {response.status_code}): {response.text}")
            log_failure(
                source="send_to_salesforce.api_error",
                error="salesforce_api_error",
                payload={
                    "status_code": response.status_code,
                    "response": response_body,
                    "business_card_data": business_card_data,
                },
            )
            return {
                "ok": False,
                "status_code": response.status_code,
                "salesforce_id": salesforce_id,
                "response": response_body,
                "error": "salesforce_api_error",
            }
    except Exception as exc:
        print(f"Error sending data to Salesforce: {exc}")
        log_failure(
            source="send_to_salesforce.exception",
            error=str(exc),
            payload={"business_card_data": business_card_data},
        )
        return {"ok": False, "error": str(exc)}


def send_to_salesforce_update(sf_id: str, payload: dict):
    """PATCH an existing Salesforce record with transcript and event data."""
    if not SALESFORCE_API_URL:
        print("Warning: SALESFORCE_API_URL not configured, skipping Salesforce update")
        log_failure(
            source="send_to_salesforce_update.not_configured",
            error="salesforce_url_not_configured",
            payload={"sf_id": sf_id, "payload": payload},
        )
        return {"ok": False, "error": "salesforce_url_not_configured"}

    try:
        # --- Relative date resolution logic ---
        from datetime import timedelta
        import re
        event = payload.get("event")
        print(f"Original event data for Salesforce update: {event}")
        meeting_datetime = None
        if isinstance(event, dict):
            date_val = event.get("date")
            time_val = event.get("time") or event.get("due_time")
            raw_text = event.get("raw_text") or ""
            # If date is missing or null, try to resolve relative date
            if (date_val is None or str(date_val).lower() == "null" or not str(date_val).strip()) and raw_text:
                match = re.search(r"(\d+)\s+days?\s*(from\s+today|later|after)?", raw_text, re.IGNORECASE)
                if match:
                    days = int(match.group(1))
                    resolved_date = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
                    event["date"] = resolved_date
                    date_val = resolved_date
                else:
                    if re.search(r"tomorrow", raw_text, re.IGNORECASE):
                        resolved_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
                        event["date"] = resolved_date
                        date_val = resolved_date
                    elif re.search(r"next week", raw_text, re.IGNORECASE):
                        resolved_date = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
                        event["date"] = resolved_date
                        date_val = resolved_date
                    else:
                        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                        for i, wd in enumerate(weekdays):
                            if re.search(wd, raw_text, re.IGNORECASE):
                                today = datetime.now(timezone.utc)
                                days_ahead = (i - today.weekday() + 7) % 7
                                if days_ahead == 0:
                                    days_ahead = 7
                                resolved_date = (today + timedelta(days=days_ahead)).date().isoformat()
                                event["date"] = resolved_date
                                date_val = resolved_date
                                break
            # If time is missing, default to 10:00:00
            if not time_val or not str(time_val).strip():
                time_val = "10:00:00"
            # If both date and time are present, build ISO datetime
            if date_val:
                meeting_datetime = f"{date_val}T{time_val}"
        # Add meetingDateTime, transcript, and leadId to payload for Salesforce update
        if meeting_datetime:
            payload["meetingDateTime"] = resolve_datetime(meeting_datetime)
        if "transcript" in payload:
            payload["transcript"] = payload["transcript"]
        payload["leadId"] = sf_id
        # --- End relative date resolution logic ---

        url = f"{SALESFORCE_API_URL}/{sf_id}"
        headers = {"Content-Type": JSON_CONTENT_TYPE}
        if SALESFORCE_COOKIE:
            headers["Cookie"] = SALESFORCE_COOKIE

        print(f"Updating Salesforce record {sf_id}: {payload}")
        response = requests.post(url, headers=headers, json=payload, timeout=15)

        response_body = None
        try:
            response_body = response.json()
        except Exception:
            response_body = response.text

        if 200 <= response.status_code < 300:
            print(f"Salesforce record {sf_id} updated successfully")
            return {"ok": True, "status_code": response.status_code, "response": response_body}
        else:
            print(f"Salesforce update error (status {response.status_code}): {response.text}")
            log_failure(
                source="send_to_salesforce_update.api_error",
                error="salesforce_api_error",
                payload={"sf_id": sf_id, "status_code": response.status_code, "response": response_body},
            )
            return {"ok": False, "status_code": response.status_code, "error": "salesforce_api_error"}
    except Exception as exc:
        print(f"Error updating Salesforce record: {exc}")
        log_failure(
            source="send_to_salesforce_update.exception",
            error=str(exc),
            payload={"sf_id": sf_id, "payload": payload},
        )
        return {"ok": False, "error": str(exc)}


def _salesforce_status_message(salesforce_result: Optional[dict]) -> str:
    """Always include SF ID in the status string so it reaches the WhatsApp reply."""
    if not salesforce_result:
        return ""
    sf_id = salesforce_result.get("salesforce_id")
    if salesforce_result.get("ok"):
        if sf_id:
            return f"Salesforce ID: {sf_id}"
        return "Salesforce updated successfully."
    error = salesforce_result.get("error", "unknown error")
    return f"Salesforce update failed: {error}"


def _to_salesforce_payload(extracted_data: dict) -> Optional[dict]:
    """Map normalized extraction output to Salesforce fields including transcript and event."""
    if not isinstance(extracted_data, dict):
        return None

    # Prefer pre-built salesforce_payload from LLM output when available
    sf_payload = extracted_data.get("salesforce_payload")
    if isinstance(sf_payload, dict) and any(sf_payload.values()):
        return sf_payload

    contact = extracted_data.get("contact") if isinstance(extracted_data.get("contact"), dict) else extracted_data
    payload = {
        "name": contact.get("full_name") or contact.get("name"),
        "designation": contact.get("designation"),
        "company": contact.get("company"),
        "phone": contact.get("phone"),
        "email": contact.get("email"),
        "website": contact.get("website"),
        "address": contact.get("address"),
    }
    if not any(payload.values()):
        return None

    transcript = extracted_data.get("transcript")
    if transcript:
        payload["transcript"] = transcript

    event = extracted_data.get("event")
    if isinstance(event, dict) and any(event.values()):
        payload["event"] = event

    return payload


def _format_extraction_reply(data: dict, kind: Optional[str]) -> str:
    """Format extracted card/audio data as a readable WhatsApp message."""
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    lines = []

    name = contact.get("full_name") or contact.get("name")
    if name:
        lines.append(f"Name: {name}")
    if contact.get("designation"):
        lines.append(f"Designation: {contact['designation']}")
    if contact.get("company"):
        lines.append(f"Company: {contact['company']}")
    if contact.get("phone"):
        lines.append(f"Phone: {contact['phone']}")
    if contact.get("email"):
        lines.append(f"Email: {contact['email']}")
    if contact.get("website"):
        lines.append(f"Website: {contact['website']}")
    if contact.get("address"):
        lines.append(f"Address: {contact['address']}")

    transcript = data.get("transcript")
    if transcript and kind == "audio":
        lines.append(f"\nTranscript: {transcript}")

    event = data.get("event")
    if isinstance(event, dict) and any(event.values()):
        event_parts = [
            v for v in [
                event.get("event_type") or event.get("type"),
                event.get("due_date") or event.get("date"),
                event.get("due_time") or event.get("time"),
            ] if v and str(v).lower() != "none"
        ]
        if event_parts:
            lines.append(f"Event: {' | '.join(event_parts)}")

    return "\n".join(lines) if lines else json.dumps(data, ensure_ascii=False)


def send_audio_message(to: str, file_path: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
    with open(file_path, "rb") as file_handle:
        files = {"file": ("reply.mp3", file_handle, "audio/mpeg")}
        params = {
            "messaging_product": "whatsapp",
            "type": "audio",
            "access_token": ACCESS_TOKEN
        }
        response = requests.post(url, params=params, files=files)

    if response.status_code == 200:
        media_id = response.json().get("id")
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id}
        }
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": JSON_CONTENT_TYPE
        }
        send_resp = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
        send_json = {}
        try:
            send_json = send_resp.json()
        except Exception:
            pass
        return {
            "ok": send_resp.status_code == 200,
            "status_code": send_resp.status_code,
            "message_id": _extract_whatsapp_message_id(send_json),
            "response": send_json,
        }
    else:
        print("Audio upload failed:", response.text)
        log_failure(
            source="send_audio_message.upload_failed",
            error="audio_upload_failed",
            phone=to,
            payload={
                "status_code": response.status_code,
                "response_text": response.text,
            },
        )
        return {"ok": False, "error": "audio_upload_failed", "status_code": response.status_code}






async def _handle_audio_event_flow(
    user_phone: str,
    media_id: str,
    incoming_message_id: str,
    sf_context: dict,
):
    """Flow 1 step 6-9: User sent audio reply to a card scan. Transcribe, extract event, update SF."""
    sf_id = sf_context["sf_id"]
    headers = {"accept": "application/json", "Content-Type": "application/json"}
    json_data = {"user_input": "", "media_id": media_id, "kind": "audio_event"}

    print(f"[Flow1] Audio follow-up for SF record {sf_id}")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{AGENT_URL}/llm-response",
            json=json_data,
            headers=headers,
            timeout=120,
        )

    if response.status_code != 200:
        print(f"[Flow1] audio_event endpoint error: {response.status_code}")
        await send_message_async(user_phone, "Could not process your voice message. Please try again.")
        return

    response_data = response.json()
    result = response_data.get("response") or {}
    transcript = result.get("transcript", "")
    event = result.get("event") or {}


    # --- Backend: resolve ISO datetime from LLM event extraction ---
    sf_update_payload = {"transcript": transcript}
    if isinstance(event, dict) and any(event.values()):
        sf_update_payload["event"] = event
        # Compose raw date/time string for resolution
        raw_date = event.get("date") or event.get("due_date")
        raw_time = event.get("time") or event.get("due_time")
        raw_text = event.get("rawText") or event.get("raw_text") or raw_date
        # Combine date and time if both present, else use raw_text
        dt_input = f"{raw_date or ''} {raw_time or ''}".strip() or raw_text
        meeting_datetime = resolve_datetime(dt_input)
        if meeting_datetime:
            sf_update_payload["meetingDateTime"] = meeting_datetime

    loop = asyncio.get_running_loop()
    sf_result = await loop.run_in_executor(None, send_to_salesforce_update, sf_id, sf_update_payload)

    clear_pending_sf_context(user_phone)

    # Save transcript + event to MongoDB contacts collection (Flow 1 follow-up)
    update_contact_event(
        phone=user_phone,
        sf_id=sf_id,
        transcript=transcript,
        event=event if isinstance(event, dict) and any(event.values()) else None,
    )

    # Save audio message to conversation thread
    audio_doc = _build_audio_message_doc(
        message_id=incoming_message_id,
        media_id=media_id,
        transcript=transcript,
        event=event if isinstance(event, dict) and any(event.values()) else None,
        salesforce_payload=sf_update_payload,
        salesforce_result=sf_result,
        reply_to=None,
    )
    upsert_conversation_message(user_phone, audio_doc)
    event_type = (event or {}).get("event_type") or (event or {}).get("type")
    event_date = (event or {}).get("due_date") or (event or {}).get("date")
    update_conversation_sf_and_context(
        user_phone,
        last_intent=event_type if event_type and event_type.lower() != "none" else None,
        last_event_date=event_date,
    )


    event_summary = ""
    meeting_datetime = sf_update_payload.get("meetingDateTime")
    if isinstance(event, dict):
        event_type = event.get("event_type") or event.get("type")
        event_date = event.get("due_date") or event.get("date")
        event_time = event.get("due_time") or event.get("time")
        if event_type and str(event_type).lower() != "none":
            parts = [event_type]
            if event_date:
                parts.append(event_date)
            if event_time:
                parts.append(event_time)
            event_summary = f"\nEvent: {' | '.join(parts)}"
            if meeting_datetime:
                event_summary += f"\nSchedule Date: {meeting_datetime}"

    sf_status = "Salesforce record updated." if (sf_result or {}).get("ok") else "Salesforce update failed."
    reply = f"Voice note received.\nTranscript: {transcript}{event_summary}\n\n{sf_status} (ID: {sf_id})"

    send_result = await loop.run_in_executor(None, send_message, user_phone, reply)
    log_event(
        event_type="outgoing_message",
        direction="outgoing",
        phone=user_phone,
        message_id=(send_result or {}).get("message_id"),
        related_message_id=incoming_message_id,
        payload={
            "kind": "audio_event",
            "sf_id": sf_id,
            "transcript": transcript,
            "event": event,
            "sf_result": sf_result,
        },
    )
    log_event(
        event_type="salesforce_sync",
        direction="system",
        phone=user_phone,
        related_message_id=incoming_message_id,
        payload={"sf_id": sf_id, "sf_update_payload": sf_update_payload, "sf_result": sf_result},
    )


async def llm_reply_to_text_v2(
    user_input: str,
    user_phone: str,
    media_id: str = None,
    kind: str = None,
    incoming_message_id: str = None,
):
    try:
        # Flow 1 step 6-9: if user sends audio and a card was recently scanned, treat as event follow-up
        if kind == "audio" and media_id:
            sf_context = get_pending_sf_context(user_phone)
            if sf_context:
                await _handle_audio_event_flow(user_phone, media_id, incoming_message_id, sf_context)
                return

        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }


        # Message context safe LLM prompt for CRM assistant
        llm_instruction = (
            "# CRM WhatsApp Assistant — Message Context Safe Prompt\n"
            "\n"
            "## SYSTEM ROLE\n"
            "You are a CRM assistant integrated with:\n"
            "* WhatsApp (messages with message_id & context_id)\n"
            "* MongoDB (message → lead mapping handled by backend)\n"
            "* Salesforce (lead creation & updates)\n"
            "\n"
            "---\n"
            "\n"
            "## 🔴 BACKEND GUARANTEE (CRITICAL)\n"
            "The backend has already:\n"
            "1. Checked if message is duplicate using `message_id`\n"
            "2. If `context_message_id` exists → resolved lead using MongoDB\n"
            "3. If no context → resolved using phone-based lookup\n"
            "4. Provided:\n"
            "   * `resolved_leadId` (FINAL lead)\n"
            "   * `is_reply_message` (true/false)\n"
            "\n"
            "👉 These values are ALWAYS correct\n"
            "👉 You MUST trust them\n"
            "\n"
            "---\n"
            "\n"
            llm_instruction = (
            "* Same text can come multiple times\n"
            "* DO NOT assume meaning based on text similarity\n"
            "* DO NOT try to detect reply manually\n"
            "\n"
            "👉 Message type is already handled by backend\n"
            "\n"
            "---\n"
            "\n"
            "## 🔴 STRICT RULES\n"
            "\n"
            "### 1. SINGLE LEAD RULE\n"
            "* ALWAYS use `resolved_leadId`\n"
            "* NEVER generate or guess Salesforce IDs\n"
            "* NEVER return multiple IDs\n"
            "\n"
            "---\n"
            "\n"
            "### 2. DO NOT HANDLE MESSAGE LOGIC\n"
            "* DO NOT use message_id\n"
            "* DO NOT use context_id\n"
            "* DO NOT compare messages\n"
            "* DO NOT infer conversation history\n"
            "\n"
            "👉 Backend already handled everything\n"
            "\n"
            "---\n"
            "\n"
            "### 3. ACTION DECISION\n"
            "\n"
            "IF `resolved_leadId` != null:\n"
            "→ action = \"update_lead\"\n"
            "→ leadId = resolved_leadId\n"
            "\n"
            "IF `resolved_leadId` == null:\n"
            "→ action = \"create_lead\"\n"
            "→ leadId = null\n"
            "\n"
            "---\n"
            "\n"
            "## 🔴 EVENT DETECTION (MANDATORY — FALLBACK SAFETY)\n"
            "\n"
            "You MUST always detect an event when the message contains intent words like:\n"
            "* meeting\n"
            "* schedule\n"
            "* call\n"
            "* follow-up\n"
            "\n"
            "Even if date or time is unclear, NEVER return an empty event {}.\n"
            "\n"
            "If user message contains intent but date/time is unclear:\n"
            "Return:\n"
            "{\n"
                "## 🔴 EVENT DATE-TIME EXTRACTION (STRICT)\n"
                "\n"
                "* If the message contains scheduling intent (meeting, call, follow-up, schedule):\n"
                "  - You MUST extract and return an event object with type and rawText.\n"
                "  - You MUST extract only the raw date and time text (e.g., 'tomorrow', 'after 2 days', 'Wednesday', '3 pm').\n"
                "  - You MUST NEVER return an ISO datetime, partial datetime, or resolved date/time.\n"
                "  - The backend will always resolve and return the ISO meetingDateTime.\n"
                "  - NEVER return meetingDateTime in your output.\n"
                "  - NEVER return partial or unresolved date strings as meetingDateTime.\n"
                "  - Always include the original message as rawText.\n"
                "\n"
                "### 🚫 STRICTLY FORBIDDEN\n"
                "* NEVER return meetingDateTime as an ISO string (e.g., '2026-04-25T10:00:00').\n"
                "* NEVER return partial datetimes (e.g., 'ThursdayT10:00:00').\n"
                "* NEVER return resolved dates (e.g., '2026-04-25').\n"
                "* NEVER return 'after 2 days' or 'Wednesday' as meetingDateTime.\n"
                "* ONLY return raw date/time text in the event object.\n"
                "\n"
                "### ✅ EXAMPLES\n"
                "Input: 'Create a meeting after 2 days'\n"
                "Output:\n"
                "{\n"
                "  'event': {\n"
                "    'type': 'meeting',\n"
                "    'date': 'after 2 days',\n"
                "    'time': null,\n"
                "    'rawText': 'Create a meeting after 2 days'\n"
                "  }\n"
                "}\n"
                "\n"
                "Input: 'Schedule a call tomorrow at 5 pm'\n"
                "Output:\n"
                "{\n"
                "  'event': {\n"
                "    'type': 'call',\n"
                "    'date': 'tomorrow',\n"
                "    'time': '5 pm',\n"
                "    'rawText': 'Schedule a call tomorrow at 5 pm'\n"
                "  }\n"
                "}\n"
                "\n"
                "---\n"
                "\n"
            "  \"event\": {\n"
            "    \"type\": \"meeting|call|follow-up|other\",\n"
            "    \"date\": null,\n"
            "    \"time\": null,\n"
            "    \"rawText\": \"<original message>\"\n"
            "  }\n"
            "}\n"
            "\n"
            "NEVER return {\"event\": {}} if intent exists.\n"
            "\n"
            "---\n"
            "\n"
            "### 🔴 NUMBER NORMALIZATION RULE\n"
            "\n"
            "Convert word numbers to digits BEFORE extracting date:\n"
            "* \"one\" → 1\n"
            "* \"two\" → 2\n"
            "* \"three\" → 3\n"
            "* \"four\" → 4\n"
            "* \"five\" → 5\n"
            "\n"
            "---\n"
            "\n"
            "### 4. EVENT EXTRACTION\n"
            "\n"
            "Extract:\n"
            "* type: meeting | call | follow-up | other\n"
            "* meetingDateTime (ISO 8601)\n"
            "* rawText\n"
            "\n"
            "Rules:\n"
            "* \"tomorrow\" → current_date + 1\n"
            "* \"in 2 days\" → current_date + 2\n"
            "* \"Tuesday\" → next upcoming Tuesday\n"
            "* If time missing → default \"10:00:00\"\n"
            "\n"
                "### 4. EVENT EXTRACTION\n"
                "\n"
                "Extract ONLY:\n"
                "* type: meeting | call | follow-up | other\n"
                "* date: raw date text (e.g., 'tomorrow', 'after 2 days', 'Wednesday')\n"
                "* time: raw time text (e.g., '3 pm', '10:30 am'), or null\n"
                "* rawText: the original message\n"
                "\n"
                "NEVER extract or return meetingDateTime.\n"
                "NEVER return resolved or partial datetimes.\n"
                "\n"
                "---\n"
                "\n"
            "### 6. RESPONSE RULES\n"
            "\n"
            "* Keep response short and professional\n"
            "* Confirm action clearly\n"
            "\n"
            "DO NOT SAY:\n"
            "* \"duplicate message\"\n"
            "* \"same message detected\"\n"
            "* \"context id used\"\n"
            "* \"MongoDB\"\n"
            "* any internal system logic\n"
            "\n"
            "---\n"
            "\n"
            "### 7. IDEMPOTENCY SAFETY\n"
            "\n"
            "* If message seems repeated or unclear:\n"
            "  → DO NOT create duplicate actions\n"
            "  → Prefer safe update or clarification\n"
            "\n"
            "---\n"
            "\n"
            "### 8. CLARIFICATION\n"
            "\n"
            "If unclear:\n"
            "→ action = \"ask_clarification\"\n"
            "\n"
            "---\n"
            "\n"
            "## INPUT\n"
            "\n"
            "Current Date:\n"
            "{{current_date}}\n"
            "\n"
            "User Message / Transcript:\n"
            "{{message}}\n"
            "\n"
            "Resolved Lead ID:\n"
            "{{resolved_leadId}}\n"
            "\n"
            "Is Reply Message:\n"
            "{{is_reply_message}}\n"
            "\n"
            "Phone:\n"
            "{{phone}}\n"
            "\n"
            "Extracted Contact:\n"
            "{{contact_json}}\n"
            "\n"
            "---\n"
            "\n"
            "## OUTPUT (STRICT JSON)\n"
            "\n"
            "{\n"
            "\"action\": \"create_lead | update_lead | ask_clarification\",\n"
            "\"leadId\": \"string or null\",\n"
            "\"intent\": \"string\",\n"
            "\"event\": {\n"
            "  \"type\": \"string or null\",\n"
            "  \"meetingDateTime\": \"ISO datetime or null\",\n"
            "  \"rawText\": \"string\"\n"
            "},\n"
            "\"entities\": {\n"
            "  \"name\": \"string or null\",\n"
            "  \"phone\": \"string or null\",\n"
            "  \"email\": \"string or null\",\n"
            "  \"company\": \"string or null\",\n"
            "  \"designation\": \"string or null\"\n"
            "},\n"
            "\"reply\": \"final response to user\"\n"
            "}\n"
            "\n"
            "---\n"
            "\n"
            "## DECISION LOGIC\n"
            "\n"
            "* Reply message → already mapped via context_id → use resolved_leadId\n"
            "* New message → already mapped via phone → use resolved_leadId\n"
            "* No lead → create new\n"
            "\n"
            "👉 You NEVER decide this yourself\n"
            "\n"
            "---\n"
            "\n"
            "## EXAMPLES\n"
            "\n"
            "### Example 1 (Same Text, Different Context)\n"
            "\n"
            "Input:\n"
            "message = \"Call me tomorrow\"\n"
            "is_reply_message = false\n"
            "\n"
            "→ treat as new intent\n"
            "\n"
            "---\n"
            "\n"
            "Input:\n"
            "message = \"Call me tomorrow\"\n"
            "is_reply_message = true\n"
            "\n"
            "→ treat as continuation (update same lead)\n"
            "\n"
            "---\n"
            "\n"
            "### Example 2 (Reply Message)\n"
            "\n"
            "resolved_leadId = \"00Qa123\"\n"
            "\n"
            "Output:\n"
            "{\n"
            "\"action\": \"update_lead\",\n"
            "\"leadId\": \"00Qa123\",\n"
            "\"intent\": \"schedule_followup\",\n"
            "\"event\": {\n"
            "  \"type\": \"follow-up\",\n"
            "  \"meetingDateTime\": \"2026-04-25T10:00:00\",\n"
            "  \"rawText\": \"tomorrow\"\n"
            "},\n"
            "\"reply\": \"Sure, I’ve scheduled a follow-up for tomorrow at 10 AM.\"\n"
            "}\n"
            "\n"
            "---\n"
            "\n"
            "## 🚨 FINAL RULE\n"
            "\n"
            "You DO NOT manage:\n"
            "* message_id\n"
            "* context_id\n"
            "* duplicate detection\n"
            "* MongoDB lookup\n"
            "* lead mapping\n"
            "\n"
            "You ONLY:\n"
            "* extract intent\n"
            "* structure data\n"
            "* generate response\n"
        )
        json_data = {
            "user_input": user_input,
            "media_id": media_id,
            "kind": kind,
            "instruction": llm_instruction,
        }

        # For plain text chat, inject conversation history so the LLM understands prior context
        if not kind or kind == "text":
            recent = get_recent_messages(user_phone, limit=10)
            if recent:
                json_data["conversation_history"] = _format_history_for_llm(recent)

        print(" LLM API request user_input:", user_input)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{AGENT_URL}/llm-response",
                json=json_data,
                headers=headers,
                timeout=120,
            )

        if response.status_code == 200 and response.headers.get("content-type", "").startswith("audio"):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
                temp_file.write(response.content)
                temp_path = temp_file.name

            try:
                loop = asyncio.get_running_loop()
                send_result = await loop.run_in_executor(None, send_audio_message, user_phone, temp_path)
                log_event(
                    event_type="outgoing_message",
                    direction="outgoing",
                    phone=user_phone,
                    message_id=(send_result or {}).get("message_id"),
                    related_message_id=incoming_message_id,
                    payload={"kind": "audio", "whatsapp_send_result": send_result},
                )
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return


        response_data = response.json()
        print("LLM API response:", response_data, "of type", type(user_phone))

        # --- If incoming_message_id and conversation_id are present, fetch transcript/event using LLM ---
        # (Assume conversation_id is passed as an argument or can be extracted from webhook payload if needed)
        # For now, we use incoming_message_id as the unique identifier
        if incoming_message_id:
            # Optionally, you can fetch the message content from your DB if needed
            # Here, we assume user_input is the message content
            # If you want to re-call OpenAI for extraction, do it here
            llm_extract_headers = {
                "accept": "application/json",
                "Content-Type": "application/json",
            }
            llm_extract_data = {
                "user_input": user_input,
                "kind": kind or "text",
            }
            print(f"Calling LLM for extraction for message_id: {incoming_message_id}")
            async with httpx.AsyncClient() as client:
                llm_extract_response = await client.post(
                    f"{AGENT_URL}/llm-response",
                    json=llm_extract_data,
                    headers=llm_extract_headers,
                    timeout=120,
                )
            if llm_extract_response.status_code == 200:
                llm_extract_json = llm_extract_response.json()
                print(f"LLM extraction result for message_id {incoming_message_id}: {llm_extract_json}")
                # Overwrite response_data with extracted details
                if llm_extract_json.get("response"):
                    response_data["response"] = llm_extract_json["response"]

        if response.status_code == 200 and response_data.get("error") is None:
            message_content = response_data.get("response")
            salesforce_payload = None
            salesforce_result = None
            sf_id = None
            message_content_str = None

            if isinstance(message_content, dict):
                salesforce_payload = _to_salesforce_payload(message_content)
                message_content_str = _format_extraction_reply(message_content, kind)
            else:
                message_content_str = message_content

            # --- Always update Salesforce lead with reply detail ---
            # Try to get sf_id from context (pending SF context or from message_content)
            sf_context = get_pending_sf_context(user_phone)
            if sf_context and sf_context.get("sf_id"):
                sf_id = sf_context["sf_id"]
            elif isinstance(message_content, dict):
                # Try to get from message_content
                sf_id = message_content.get("salesforce_id") or message_content.get("sf_id")

            # If we have an sf_id, update the lead with the reply detail
            if sf_id:
                update_payload = {}
                if kind == "audio" and isinstance(message_content, dict):
                    update_payload["transcript"] = message_content.get("transcript")
                    event = message_content.get("event") if isinstance(message_content.get("event"), dict) else None
                    if event:
                        update_payload["event"] = event
                elif kind == "text" or not kind:
                    update_payload["transcript"] = user_input
                # Optionally, merge in more fields if needed
                loop = asyncio.get_running_loop()
                salesforce_result = await loop.run_in_executor(None, send_to_salesforce_update, sf_id, update_payload)
                sf_status = _salesforce_status_message(salesforce_result)
            elif salesforce_payload:
                # Fallback: create new if no sf_id
                loop = asyncio.get_running_loop()
                salesforce_result = await loop.run_in_executor(None, send_to_salesforce, salesforce_payload)
                sf_id = (salesforce_result or {}).get("salesforce_id")
                if sf_id:
                    salesforce_payload["salesforce_id"] = sf_id
                sf_status = _salesforce_status_message(salesforce_result)
            else:
                sf_status = None

            # Save extracted contact + SF ID to legacy contacts collection
            if isinstance(message_content, dict):
                _contact_data = dict(message_content)
                if sf_id:
                    _contact_data["salesforce_id"] = sf_id
                loop.run_in_executor(None, lambda: save_contact(
                    phone=user_phone,
                    extracted_data=_contact_data,
                    sf_id=sf_id,
                    source=kind,
                    message_id=incoming_message_id,
                ))

            # Save to new conversation thread
            if kind == "image" and isinstance(message_content, dict):
                img_doc = _build_image_message_doc(
                    message_id=incoming_message_id,
                    media_id=media_id,
                    extracted_data=message_content,
                    salesforce_payload=salesforce_payload,
                    salesforce_result=salesforce_result,
                )
                loop.run_in_executor(None, lambda: upsert_conversation_message(user_phone, img_doc))
                if sf_id:
                    loop.run_in_executor(None, lambda: update_conversation_sf_and_context(user_phone, sf_id=sf_id))

            elif kind == "audio" and isinstance(message_content, dict):
                transcript = message_content.get("transcript") or ""
                event = message_content.get("event") if isinstance(message_content.get("event"), dict) else None
                audio_doc = _build_audio_message_doc(
                    message_id=incoming_message_id,
                    media_id=media_id,
                    transcript=transcript,
                    event=event,
                    salesforce_payload=salesforce_payload,
                    salesforce_result=salesforce_result,
                )
                loop.run_in_executor(None, lambda: upsert_conversation_message(user_phone, audio_doc))
                if event:
                    _ev_type = event.get("event_type") or event.get("type")
                    _ev_date = event.get("due_date") or event.get("date")
                    loop.run_in_executor(None, lambda: update_conversation_sf_and_context(
                        user_phone,
                        last_intent=_ev_type if _ev_type and _ev_type.lower() != "none" else None,
                        last_event_date=_ev_date,
                    ))

            # Flow 1: after image card sync, store SF ID so the user can follow up with voice
            if kind == "image" and salesforce_result and salesforce_result.get("ok") and sf_id:
                store_pending_sf_context(
                    user_phone,
                    sf_id,
                    message_content if isinstance(message_content, dict) else {},
                )
                sf_status = f"{sf_status}\nReply with a voice note to add a follow-up event."

            if sf_status:
                message_content_str = f"{message_content_str}\n\n{sf_status}" if message_content_str else sf_status

            print(f"LLM API message content: {message_content_str}")
            if message_content_str:
                loop = asyncio.get_running_loop()
                print(f"Sending message to {user_phone}: {message_content_str}")
                send_result = await loop.run_in_executor(None, send_message, user_phone, message_content_str)
                if not (send_result or {}).get("ok"):
                    log_failure(
                        source="llm_reply_to_text_v2.send_message_failed",
                        error=(send_result or {}).get("error", "whatsapp_send_failed"),
                        phone=user_phone,
                        related_message_id=incoming_message_id,
                        payload={"reply": message_content_str, "send_result": send_result},
                    )
                log_event(
                    event_type="outgoing_message",
                    direction="outgoing",
                    phone=user_phone,
                    message_id=(send_result or {}).get("message_id"),
                    related_message_id=incoming_message_id,
                    payload={
                        "kind": kind or "text",
                        "sf_id": (salesforce_result or {}).get("salesforce_id"),
                        "reply": message_content_str,
                        "salesforce_payload": salesforce_payload,
                        "salesforce_result": salesforce_result,
                        "whatsapp_send_result": send_result,
                    },
                )

                if salesforce_result:
                    _sf_id = (salesforce_result or {}).get("salesforce_id")
                    log_event(
                        event_type="salesforce_sync",
                        direction="system",
                        phone=user_phone,
                        related_message_id=incoming_message_id,
                        payload={
                            "sf_id": _sf_id,
                            "kind": kind,
                            "salesforce_payload": salesforce_payload,
                            "salesforce_result": salesforce_result,
                        },
                    )

                # Save user + assistant text messages to conversation thread
                if not kind or kind == "text":
                    now_ts = _utc_now_iso()
                    user_msg_doc = {
                        "message_id": incoming_message_id,
                        "type": "text",
                        "direction": "user",
                        "timestamp": now_ts,
                        "content": user_input,
                    }
                    assistant_msg_doc = {
                        "message_id": (send_result or {}).get("message_id"),
                        "type": "text",
                        "direction": "assistant",
                        "timestamp": _utc_now_iso(),
                        "content": message_content_str,
                    }
                    loop.run_in_executor(None, lambda: upsert_conversation_message(user_phone, user_msg_doc))
                    loop.run_in_executor(None, lambda: upsert_conversation_message(user_phone, assistant_msg_doc))
            else:
                print("Error: Empty message content from LLM API")
                log_failure(
                    source="llm_reply_to_text_v2.empty_response",
                    error="empty_response",
                    phone=user_phone,
                    related_message_id=incoming_message_id,
                    payload={"llm_response": response_data},
                )
                await send_message_async(user_phone, "Received empty response from LLM API.")
        else:
            print("Error: Invalid LLM API response", response_data)
            log_failure(
                source="llm_reply_to_text_v2.invalid_response",
                error="invalid_llm_response",
                phone=user_phone,
                related_message_id=incoming_message_id,
                payload={"status_code": response.status_code, "response_data": response_data},
            )
            await send_message_async(user_phone, "Failed to process message due to an internal server error.")

    except Exception as e:
        print("LLM error:", e)
        log_failure(
            source="llm_reply_to_text_v2.exception",
            error=str(e),
            phone=user_phone,
            related_message_id=incoming_message_id,
            payload={"user_input": user_input, "media_id": media_id, "kind": kind},
        )
        try:
            await send_message_async(user_phone, "Sorry, something went wrong while generating a response.")
        except Exception as send_err:
            print("Failed to send error reply:", send_err)
            log_failure(
                source="llm_reply_to_text_v2.error_reply_failed",
                error=str(send_err),
                phone=user_phone,
                related_message_id=incoming_message_id,
            )
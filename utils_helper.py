"""
utils_helper.py: Common helpers for WhatsApp LLM bot flows
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from conversation_store import store_sf_message_link, get_sf_id_by_message_id

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12
}

def clean_value(val):
    if not val:
        return None
    val = str(val).strip().lower()
    if val in ["null", "none", ""]:
        return None
    return val

def resolve_datetime(text: str) -> Optional[str]:
    if not text:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text):
        return text
    text = normalize_numbers(text.lower())
    now = datetime.now(timezone.utc)
    base_date = None
    # Relative dates
    if "day after tomorrow" in text:
        base_date = now + timedelta(days=2)
    elif "tomorrow" in text:
        base_date = now + timedelta(days=1)
    elif "today" in text:
        base_date = now
    elif m := re.search(r"after (\d+) days", text):
        base_date = now + timedelta(days=int(m.group(1)))
    elif m := re.search(r"in (\d+) days", text):
        base_date = now + timedelta(days=int(m.group(1)))
    elif "next week" in text:
        base_date = now + timedelta(days=7)
    elif m := re.search(r"(\d+) day", text):
        base_date = now + timedelta(days=int(m.group(1)))
    elif m := re.search(r"(\d+) days", text):
        base_date = now + timedelta(days=int(m.group(1)))
    # Date (26th)
    elif m := re.search(r"\b(\d{1,2})(st|nd|rd|th)\b", text):
        day = int(m.group(1))
        try:
            base_date = now.replace(day=day)
            if base_date < now:
                month = now.month + 1 if now.month < 12 else 1
                year = now.year if now.month < 12 else now.year + 1
                base_date = base_date.replace(year=year, month=month)
        except:
            base_date = None
    # Date (26 April)
    elif m := re.search(r"(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)", text):
        day = int(m.group(1))
        month = MONTHS[m.group(2)]
        year = now.year
        try:
            base_date = datetime(year, month, day)
            if base_date < now:
                base_date = datetime(year + 1, month, day)
        except:
            base_date = None
    # Weekday
    else:
        for wd in WEEKDAYS:
            if re.search(rf"\b(this\s+|next\s+|on\s+|after\s+)?{wd}\b", text):
                today_idx = now.weekday()
                target_idx = WEEKDAYS.index(wd)
                days_ahead = (target_idx - today_idx + 7) % 7
                days_ahead = days_ahead or 7
                base_date = now + timedelta(days=days_ahead)
                break
    if not base_date:
        return None
    # Time
    hour, minute = 10, 0
    if m := re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text):
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if m.group(3) == "pm" and hour != 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
    elif m := re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text):
        hour = int(m.group(1))
        minute = int(m.group(2))
    elif m := re.search(r"\bat (\d{1,2})\b", text):
        hour = int(m.group(1))
    elif "morning" in text:
        hour = 10
    elif "afternoon" in text:
        hour = 14
    elif "evening" in text:
        hour = 18
    elif "night" in text:
        hour = 21
    dt = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

def normalize_numbers(text: str) -> str:
    word_map = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"
    }
    for word, num in word_map.items():
        text = re.sub(rf"\b{word}\b", num, text, flags=re.IGNORECASE)
    return text

def build_salesforce_payload(extracted_data: dict, transcript: Optional[str] = None, event: Optional[dict] = None, s3_url: Optional[str] = None) -> dict:
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
    if transcript:
        payload["transcript"] = transcript
    if event:
        payload["event"] = event
    if s3_url:
        payload["s3_url"] = s3_url
    return payload

def store_reply_mapping(sf_id: str, message_id: str, payload: dict):
    store_sf_message_link(sf_id, message_id, payload)

def fetch_sf_id_by_message_id(message_id: str) -> Optional[str]:
    return get_sf_id_by_message_id(message_id)

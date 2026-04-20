import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "whatsapp_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "conversation_events")
MONGODB_FAILURE_COLLECTION = os.getenv("MONGODB_FAILURE_COLLECTION", "conversation_failures")
_SF_CONTEXT_COLLECTION = os.getenv("MONGODB_SF_CONTEXT_COLLECTION", "pending_sf_contexts")
_SF_CONTEXT_TTL_SECONDS = int(os.getenv("SF_CONTEXT_TTL_SECONDS", "86400"))
_CONTACTS_COLLECTION = os.getenv("MONGODB_CONTACTS_COLLECTION", "contacts")

_LOCK = threading.Lock()
_CLIENT: Optional[MongoClient] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_client() -> MongoClient:
    global _CLIENT
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not configured")
    if _CLIENT is None:
        _CLIENT = MongoClient(MONGODB_URI)
        _CLIENT.admin.command("ping")
    return _CLIENT


def _get_collection():
    return _get_client()[MONGODB_DB][MONGODB_COLLECTION]


def _get_failure_collection():
    return _get_client()[MONGODB_DB][MONGODB_FAILURE_COLLECTION]


def _get_sf_context_collection():
    return _get_client()[MONGODB_DB][_SF_CONTEXT_COLLECTION]


def _get_contacts_collection():
    return _get_client()[MONGODB_DB][_CONTACTS_COLLECTION]


# ── Event logging ─────────────────────────────────────────────────────────────

def log_event(
    *,
    event_type: str,
    phone: Optional[str],
    direction: Optional[str] = None,
    message_id: Optional[str] = None,
    related_message_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Append one immutable event document to MongoDB."""
    record = {
        "timestamp": _utc_now_iso(),
        "event_type": event_type,
        "direction": direction,
        "phone": phone,
        "message_id": message_id,
        "related_message_id": related_message_id,
        "payload": payload or {},
    }
    with _LOCK:
        try:
            _get_collection().insert_one(record)
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB log_event failed: {exc}")


def log_failure(
    *,
    source: str,
    error: str,
    phone: Optional[str] = None,
    message_id: Optional[str] = None,
    related_message_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Append one failure document to dedicated MongoDB failure collection."""
    record = {
        "timestamp": _utc_now_iso(),
        "source": source,
        "error": error,
        "phone": phone,
        "message_id": message_id,
        "related_message_id": related_message_id,
        "payload": payload or {},
    }
    with _LOCK:
        try:
            _get_failure_collection().insert_one(record)
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB log_failure failed: {exc}")


def get_conversation_history(phone: str, limit: int = 20):
    """Return the most recent conversation events for a given phone number."""
    return list(
        _get_collection()
        .find({"phone": phone})
        .sort("timestamp", -1)
        .limit(limit)
    )


# ── Pending Salesforce context (Flow 1) ───────────────────────────────────────

def store_pending_sf_context(phone: str, sf_id: str, card_json: dict) -> None:
    """Upsert pending SF context for a user so a follow-up voice note can update the record."""
    with _LOCK:
        try:
            _get_sf_context_collection().update_one(
                {"phone": phone},
                {"$set": {
                    "phone": phone,
                    "sf_id": sf_id,
                    "card_json": card_json,
                    "created_at": _utc_now_iso(),
                }},
                upsert=True,
            )
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB store_pending_sf_context failed: {exc}")


def get_pending_sf_context(phone: str) -> Optional[dict]:
    """Return pending SF context if it exists and is within TTL, else None."""
    with _LOCK:
        try:
            col = _get_sf_context_collection()
            doc = col.find_one({"phone": phone})
            if not doc:
                return None
            created_at = doc.get("created_at")
            if created_at:
                created_dt = datetime.fromisoformat(created_at)
                age = (datetime.now(timezone.utc) - created_dt).total_seconds()
                if age > _SF_CONTEXT_TTL_SECONDS:
                    col.delete_one({"phone": phone})
                    return None
            return {"sf_id": doc.get("sf_id"), "card_json": doc.get("card_json")}
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB get_pending_sf_context failed: {exc}")
            return None


def clear_pending_sf_context(phone: str) -> None:
    """Delete pending SF context after the follow-up voice note has been processed."""
    with _LOCK:
        try:
            _get_sf_context_collection().delete_one({"phone": phone})
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB clear_pending_sf_context failed: {exc}")


# ── Contacts store ────────────────────────────────────────────────────────────

def save_contact(
    *,
    phone: str,
    extracted_data: dict,
    sf_id: Optional[str] = None,
    source: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """
    Upsert an extracted contact record into the contacts collection.
    Called after every successful image / audio / contact extraction.
    """
    contact = extracted_data.get("contact") if isinstance(extracted_data.get("contact"), dict) else {}
    now = _utc_now_iso()
    record = {
        "phone": phone,
        "sf_id": sf_id,
        "source": source,
        "message_id": message_id,
        "contact": contact,
        "transcript": extracted_data.get("transcript"),
        "event": extracted_data.get("event"),
        "intent": extracted_data.get("intent"),
        "metadata": extracted_data.get("metadata"),
        "updated_at": now,
    }
    with _LOCK:
        try:
            col = _get_contacts_collection()
            col.update_one(
                {"phone": phone, "message_id": message_id},
                {"$set": record, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            print(f"[MongoDB] contact saved for phone={phone} sf_id={sf_id}")
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB save_contact failed: {exc}")


def update_contact_event(
    *,
    phone: str,
    sf_id: str,
    transcript: str,
    event: Optional[dict],
) -> None:
    """
    Update an existing contact record with transcript and event after a voice follow-up (Flow 1).
    Matches by sf_id so the correct card record is updated.
    """
    now = _utc_now_iso()
    with _LOCK:
        try:
            col = _get_contacts_collection()
            col.update_one(
                {"phone": phone, "sf_id": sf_id},
                {"$set": {
                    "transcript": transcript,
                    "event": event,
                    "updated_at": now,
                }},
            )
            print(f"[MongoDB] contact event updated for phone={phone} sf_id={sf_id}")
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB update_contact_event failed: {exc}")

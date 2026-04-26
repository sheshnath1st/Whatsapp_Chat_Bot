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
_CONVERSATIONS_COLLECTION = os.getenv("MONGODB_CONVERSATIONS_COLLECTION", "conversations")

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


def _get_conversations_collection():
    return _get_client()[MONGODB_DB][_CONVERSATIONS_COLLECTION]


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


# ── Thread-per-user conversation store ───────────────────────────────────────

def upsert_conversation_message(phone: str, message_doc: dict) -> None:
    """
    Append a typed message document to the user's conversation thread.
    Creates the conversation document if it does not yet exist.

    message_doc must include at minimum: message_id, type, timestamp.
    """
    now = _utc_now_iso()
    # Patch: Save context_id if present in message_doc
    context_id = message_doc.get("context_id")
    if context_id:
        message_doc["context_id"] = context_id
    with _LOCK:
        try:
            _get_conversations_collection().update_one(
                {"_id": f"conv_{phone}"},
                {
                    "$push": {"messages": message_doc},
                    "$set": {"updated_at": now},
                    "$setOnInsert": {
                        "user": phone,
                        "sf_id": None,
                        "created_at": now,
                        "context": {"last_intent": None, "last_event_date": None},
                    },
                },
                upsert=True,
            )
            print(f"[MongoDB] conversation message saved for phone={phone} type={message_doc.get('type')}")
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB upsert_conversation_message failed: {exc}")


def find_sf_id_by_context_id(phone: str, context_id: str, wa_id: str = None, user_id: str = None) -> Optional[str]:
    """
    Search the conversation messages for a message with message_id == context_id and return its sf_id if present.
    Uses wa_id/user_id as the unique key if provided.
    """
    conv = get_conversation(phone, context_id=context_id, user_id=user_id)
    if conv:
        messages = conv.get("messages") or []
        for msg in messages:
            if msg.get("message_id") == context_id:
                # Try to get sf_id from message or its salesforce field
                sf_id = msg.get("sf_id")
                if not sf_id:
                    sf = (msg.get("salesforce") or {}).get("response") or {}
                    sf_id = sf.get("sf_id") or sf.get("salesforce_id")
                return sf_id

    # Fallback: search event log for salesforce_sync with related_message_id == context_id
    col = _get_collection()
    event = col.find_one({
        "event_type": "salesforce_sync",
        "related_message_id": context_id
    })
    if event:
        payload = event.get("payload") or {}
        # Try to get sf_id from payload or sf_result
        sf_id = payload.get("sf_id")
        if not sf_id:
            sf_result = payload.get("sf_result") or {}
            sf_id = sf_result.get("salesforce_id") or sf_result.get("sf_id")
        return sf_id
    return None


def update_conversation_sf_and_context(
    phone: str,
    *,
    sf_id: Optional[str] = None,
    last_intent: Optional[str] = None,
    last_event_date: Optional[str] = None,
) -> None:
    """Update the top-level sf_id and/or context fields on the conversation."""
    now = _utc_now_iso()
    fields: dict[str, Any] = {"updated_at": now}
    if sf_id is not None:
        fields["sf_id"] = sf_id
    if last_intent is not None:
        fields["context.last_intent"] = last_intent
    if last_event_date is not None:
        fields["context.last_event_date"] = last_event_date
    with _LOCK:
        try:
            _get_conversations_collection().update_one(
                {"_id": f"conv_{phone}"},
                {"$set": fields},
            )
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB update_conversation_sf_and_context failed: {exc}")



def get_conversation(phone: str, context_id: str = None, user_id: str = None) -> Optional[dict]:
    """
    Return the full conversation document for a user. Prefer WhatsApp wa_id or user_id for uniqueness.
    If wa_id or user_id is provided, use it as the unique key. Fallback to phone if not.
    """
    # Prefer user_id, then wa_id, then phone for unique conversation key

    with _LOCK:
        try:
            if context_id:
                return _get_conversations_collection().find_one({"message_id": context_id})
            elif user_id:
                return _get_conversations_collection().find_one({"user_id": user_id})
            else:
                return _get_conversations_collection().find_one({"_id": f"conv_{phone}"})
        except (RuntimeError, PyMongoError) as exc:
            print(f"MongoDB get_conversation failed: {exc}")
            return None


def get_recent_messages(phone: str, limit: int = 10) -> list:
    """Return the last N messages from the conversation thread."""
    conv = get_conversation(phone)
    if not conv:
        return []
    messages = conv.get("messages") or []
    return messages[-limit:] if len(messages) > limit else messages


# ── Salesforce message link store ────────────────────────────────────────────

def store_sf_message_link(sf_id: str, message_id: str, payload: dict) -> None:
    """
    Store a mapping of Salesforce ID, WhatsApp message ID, and payload in a dedicated collection.
    """
    try:
        col = _get_client()[MONGODB_DB]["sf_message_links"]
        doc = {
            "sf_id": sf_id,
            "message_id": message_id,
            "payload": payload,
            "created_at": _utc_now_iso(),
        }
        col.update_one(
            {"message_id": message_id},
            {"$set": doc},
            upsert=True,
        )
        print(f"[MongoDB] sf_message_link saved for sf_id={sf_id} message_id={message_id}")
    except Exception as exc:
        print(f"MongoDB store_sf_message_link failed: {exc}")

def get_sf_id_by_message_id(message_id: str) -> Optional[str]:
    """
    Fetch the Salesforce ID for a given WhatsApp message ID from the sf_message_links collection.
    """
    try:
        col = _get_client()[MONGODB_DB]["sf_message_links"]
        doc = col.find_one({"message_id": message_id})
        print(f"[MongoDB] get_sf_id_by_message_id for message_id={message_id} returned doc={doc}")
        if doc:
            return doc.get("sf_id")
        return None
    except Exception as exc:
        print(f"MongoDB get_sf_id_by_message_id failed: {exc}")
        return None

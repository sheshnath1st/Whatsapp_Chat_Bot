# Fetch recent conversation history for a phone number
def get_conversation_history(phone: str, limit: int = 20):
    """Return the most recent conversation events for a given phone number."""
    collection = _get_collection()
    return list(collection.find({"phone": phone}).sort("timestamp", -1).limit(limit))
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import MongoClient
from pymongo.errors import PyMongoError

_LOCK = threading.Lock()
_CLIENT: Optional[MongoClient] = None

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "whatsapp_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "conversation_events")
MONGODB_FAILURE_COLLECTION = os.getenv("MONGODB_FAILURE_COLLECTION", "conversation_failures")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_collection():
    """Return MongoDB collection for conversation events."""
    global _CLIENT

    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not configured")

    if _CLIENT is None:
        _CLIENT = MongoClient(MONGODB_URI)
        _CLIENT.admin.command("ping")

    return _CLIENT[MONGODB_DB][MONGODB_COLLECTION]


def _get_failure_collection():
    """Return MongoDB collection for failure events."""
    global _CLIENT

    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not configured")

    if _CLIENT is None:
        _CLIENT = MongoClient(MONGODB_URI)
        _CLIENT.admin.command("ping")

    return _CLIENT[MONGODB_DB][MONGODB_FAILURE_COLLECTION]


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
            collection = _get_collection()
            collection.insert_one(record)
        except (RuntimeError, PyMongoError) as exc:
            # Keep webhook flow alive even if MongoDB is temporarily unavailable.
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
            collection = _get_failure_collection()
            collection.insert_one(record)
        except (RuntimeError, PyMongoError) as exc:
            # Keep runtime alive even if failure logging is unavailable.
            print(f"MongoDB log_failure failed: {exc}")

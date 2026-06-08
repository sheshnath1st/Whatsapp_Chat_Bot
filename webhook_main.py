from importlib.metadata import metadata
import json

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from webhook_utils import llm_reply_to_text_v2
from conversation_store import log_event, log_failure
from media_store import upload_whatsapp_media_to_s3
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# Ensure logs directory exists and configure rotating file logging
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_file_path = os.path.join(LOG_DIR, "app.log")

formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Add console handler if none present
if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

# Add rotating file handler (5MB per file, keep 5 backups)
file_handler = RotatingFileHandler(log_file_path, maxBytes=5 * 1024 * 1024, backupCount=5)
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)
app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
class WhatsAppMessage(BaseModel):
    object: str
    entry: list

from conversation_store import (
    log_event,
    log_failure,
    get_pending_sf_context,
)

def _extract_user_message(message: dict):

    if "text" in message:
        return (
            message.get("text", {}).get("body", ""),
            None,
            "text"
        )

    elif "image" in message:
        image = message.get("image", {}) or {}
        return (
            image.get("caption", ""),
            image.get("id"),
            "image"
        )

    elif "document" in message:
        document = message.get("document", {}) or {}
        return (
            document.get("caption", ""),
            document.get("id"),
            "document"
        )

    elif "video" in message:
        video = message.get("video", {}) or {}
        return (
            video.get("caption", ""),
            video.get("id"),
            "video"
        )

    elif "audio" in message:
        audio = message.get("audio", {}) or {}
        return (
            "",
            audio.get("id"),
            "audio"
        )

    return ("", None, None)


def _log_status_events(change: dict, incoming_phone_id: Optional[str]) -> int:
    statuses = change.get("statuses") or []
    count = 0
    for status in statuses:
        recipient = status.get("recipient_id")
        msg_id = status.get("id")
        status_value = status.get("status")
        log_event(
            event_type="message_status",
            direction="outgoing",
            phone=recipient,
            message_id=msg_id,
            payload={
                "status": status_value,
                "phone_number_id": incoming_phone_id,
                "raw_status": status,
            },
        )
        count += 1
    return count


def _process_incoming_messages(
    *,
    change: dict,
    incoming_phone_id: Optional[str],
    background_tasks: BackgroundTasks,
) -> int:
    from external_api_service import ExternalApiService
    import requests
    import mimetypes
    handled_messages = 0
    messages = change.get("messages") or []
    print(f"Processing {len(messages)} incoming messages...")
    for idx, message in enumerate(messages):
        print(f"\n--- Handling message {idx+1}/{len(messages)} ---")
        user_phone = message.get("from")
        incoming_message_id = message.get("id")

        user_message, media_id, kind = _extract_user_message(message)
        print(f"Extracted user_message: {user_message}")
        print(f"Extracted media_id: {media_id}")
        print(f"Detected kind: {kind}")
        # print(f"Is forwarded message: {is_forwarded_message(message)}")
        # is_forwarded = is_forwarded_message(message)
        # print(f"Is forwarded message: {is_forwarded}")
        try:
            if message.get("context") is not None and not message["context"].get("forwarded", False):
                context_object = message.get("context", {})
                context_from = context_object.get("from", user_phone)
                context_id = context_object.get("id", incoming_message_id)
                logger.info(
                f"Processing forwarded message. "
                f"phone={user_phone}, "
                f"message_id={incoming_message_id}"
                f"context_from={context_from}, "
                f"context_id={context_id}")
                api_response = ExternalApiService.update_message_tag(
                    message_id=context_id,
                    tag=context_from
                )
                reply_text = api_response
                return handled_messages

            elif kind == "text":
                reply_text = ExternalApiService.send_text(
                    user_message,
                    incoming_message_id,
                    user_phone
                )
            elif kind is None:  # Text
                reply_text = ExternalApiService.send_text(user_message, incoming_message_id, user_phone)
            # elif kind in {"image", "document", "video", "audio"} and media_id:
            elif kind in {"image"} and media_id:
                # Download media from WhatsApp
                from media_store import _fetch_media_download_url, _download_media_bytes
                media_url = _fetch_media_download_url(media_id)
                if not media_url:
                    log_failure(source="media_download", error="Failed to fetch media URL", phone=user_phone, message_id=incoming_message_id)
                    reply_text = "Sorry, your media could not be processed."
                else:
                    media_bytes, content_type = _download_media_bytes(media_url)
                    if not media_bytes or not content_type:
                        log_failure(source="media_download", error="Failed to download media bytes", phone=user_phone, message_id=incoming_message_id)
                        reply_text = "Sorry, your media could not be processed."
                    else:
                        ext = mimetypes.guess_extension(content_type) or (".jpg" if kind=="image" else ".ogg")
                        filename = f"{media_id}{ext}"
                        # reply_text = ExternalApiService.send_media(media_bytes, filename, content_type, incoming_message_id, user_phone, kind)
                        reply_text = ExternalApiService.send_media(
                            media_bytes=media_bytes,
                            filename=filename,
                            mimetype=content_type,
                            message_id=incoming_message_id,
                            phone_number=user_phone,
                            raw_message=user_message
                        )
                        print(f"Received reply from API for media message: {reply_text}")
            else:
                reply_text = "Sorry, this message type is not supported."
        except Exception as exc:
            log_failure(source="external_api", error=str(exc), phone=user_phone, message_id=incoming_message_id)
            reply_text = "Sorry, I am unable to process your request right now. Please try again later."

        # Send reply to WhatsApp
        if reply_text is None:
            reply_messages = [{
                "phone_number": user_phone,
                "message_id": incoming_message_id,
                "text": (
                    "⚠️ Unable to process your request right now. "
                    "Please try again later."
                )
            }]
        else:
            reply_messages = build_whatsapp_messages(reply_text)
        from webhook_utils import send_message_async
        import asyncio
        loop = asyncio.get_event_loop()
        for reply_message in reply_messages:
            loop.create_task(
                send_message_async(
                    reply_message.get("phone_number") or user_phone,
                    reply_message.get("text"),
                    reply_to_message_id=
                        reply_message.get("message_id")
                        or incoming_message_id
                )
            )
        # loop.create_task(send_message_async(user_phone, reply_text or "Sorry, I am unable to process your request right now. Please try again later.",reply_to_message_id = incoming_message_id))
        handled_messages += 1
    print(f"Total handled messages: {handled_messages}")
    return handled_messages

def is_forwarded_message(message: dict) -> bool:
    context = message.get("context", {})

    return (
        context.get("forwarded", False)
        or context.get("frequently_forwarded", False)
    )

def build_whatsapp_messages(api_response):

    matches = api_response.get("matches", [])

    if not matches:
        return [{
            "phone_number": api_response.get("phone_number"),
            "message_id": api_response.get("message_id"),
            "text": api_response.get(
                "reply_message",
                "❌ No matching buyers found."
            )
        }]

    messages = []

    for idx, match in enumerate(matches[:10], start=1):

        buyer = match.get("buy_snapshot", {})
        broker = match.get("buy_broker", {})

        msg = (
            f"🎯 Buyer Match #{idx}\n\n"
            f"🏠 {buyer.get('bhk')}BR "
            f"{buyer.get('property_type', 'Property').title()}\n"
            f"📍 {buyer.get('location', 'N/A')}\n"
            f"📞 {broker.get('phone', 'N/A')}"
        )

        messages.append({
            "phone_number": match.get("phone_number"),
            "message_id": match.get("message_id"),
            "text": msg
        })

    return messages


def _is_ignored_phone(incoming_phone_id: Optional[str]) -> bool:
    return bool(PHONE_NUMBER_ID and incoming_phone_id != PHONE_NUMBER_ID)


def _contact_to_extraction_text(contact: dict) -> str:
    """Convert WhatsApp contact payload to a compact text block for extraction."""
    name_obj = contact.get("name", {}) or {}
    org_obj = contact.get("org", {}) or {}
    phones = contact.get("phones", []) or []
    emails = contact.get("emails", []) or []
    urls = contact.get("urls", []) or []
    addresses = contact.get("addresses", []) or []

    name = (
        name_obj.get("formatted_name")
        or " ".join(
            part for part in [name_obj.get("first_name"), name_obj.get("last_name")] if part
        )
        or ""
    )
    designation = org_obj.get("title") or ""
    company = org_obj.get("company") or ""
    phone = ", ".join(item.get("phone", "") for item in phones if item.get("phone"))
    email = ", ".join(item.get("email", "") for item in emails if item.get("email"))
    website = ", ".join(item.get("url", "") for item in urls if item.get("url"))

    address_parts = []
    for addr in addresses:
        parts = [
            addr.get("street"),
            addr.get("city"),
            addr.get("state"),
            addr.get("country"),
            addr.get("zip"),
        ]
        address_parts.append(", ".join(part for part in parts if part))
    address = " | ".join(part for part in address_parts if part)

    return (
        "Extract business card fields from this contact data:\n"
        f"Name: {name}\n"
        f"Designation: {designation}\n"
        f"Company: {company}\n"
        f"Phone: {phone}\n"
        f"Email: {email}\n"
        f"Website: {website}\n"
        f"Address: {address}"
    )


@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        # Echo the challenge verbatim for Meta webhook verification.
        return Response(content=str(challenge), media_type="text/plain")

    return JSONResponse(status_code=403, content={"error": "Invalid verification token"})





@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
    except Exception as exc:
        logger.warning("Malformed webhook payload: %r", exc)
        log_failure(
            source="webhook_handler.parse_json",
            error=str(exc),
            payload={"detail": "Malformed webhook payload"},
        )
        resp_content = {"status": "bad_request"}
        logger.info("Webhook request parsing failed; response=%s", resp_content)
        return JSONResponse(status_code=200, content=resp_content)

    try:
        logger.info("Received webhook data: %s", json.dumps(data))
        message_data = WhatsAppMessage(**data)

        if not message_data.entry:
            resp_content = {"status": "no_entry"}
            logger.info("No entry in webhook payload; response=%s", resp_content)
            return JSONResponse(status_code=200, content=resp_content)

        handled_messages = 0
        handled_statuses = 0
        for entry in message_data.entry:
            changes = entry.get("changes") or []
            for change_item in changes:
                change = change_item.get("value", {})
                metadata = change.get("metadata", {})
                incoming_phone_id = metadata.get("phone_number_id")

                if _is_ignored_phone(incoming_phone_id) or change.get("messages") is None:
                    print(f"❌ Ignored webhook for phone_number_id: {incoming_phone_id}")
                    continue

                if _is_ignored_phone(incoming_phone_id):
                    print(f"❌ Ignored webhook for phone_number_id: {incoming_phone_id}")
                    continue

                # Ignore status updates (sent, delivered, read)
                if "messages" not in change:
                    print("❌ Ignored non-message event")
                    continue
                print(f"✅ Processing webhook for phone_number_id: {incoming_phone_id}")
                print(f"Webhook change content: {json.dumps(change)}")
                handled_statuses += _log_status_events(change, incoming_phone_id)
                handled_messages += _process_incoming_messages(
                    change=change,
                    incoming_phone_id=incoming_phone_id,
                    background_tasks=background_tasks,
                )

        if handled_messages == 0 and handled_statuses == 0:
            resp_content = {"status": "no_relevant_event"}
            logger.info(
                "Webhook processed but no relevant events; handled_messages=%d handled_statuses=%d response=%s",
                handled_messages,
                handled_statuses,
                resp_content,
            )
            return JSONResponse(status_code=200, content=resp_content)

        resp_content = {"status": "ok", "handled_messages": handled_messages, "handled_statuses": handled_statuses}
        logger.info("Webhook processed successfully; response=%s", resp_content)
        return JSONResponse(status_code=200, content=resp_content)

    except Exception as exc:
        logger.exception("Unhandled error in webhook_handler: %r", exc)
        log_failure(
            source="webhook_handler.unhandled",
            error=str(exc),
            payload={"webhook_payload": data if isinstance(data, dict) else {}},
        )
        # Always return 200 so Meta does not retry the delivery.
        resp_content = {"status": "error"}
        logger.info("Webhook handler unhandled exception; response=%s", resp_content)
        return JSONResponse(status_code=200, content=resp_content)
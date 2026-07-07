from importlib.metadata import metadata
import json

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from webhook_utils import llm_reply_to_text_v2
from project_interactive import send_project_interactive_message, build_project_buttons, handle_project_button_click
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
    upsert_conversation_message,
)
from datetime import datetime, timezone
import threading
import asyncio

# In-memory caches. Replace with Redis or persistent store in production.
PROCESSED_MESSAGES = set()
PROCESSED_MESSAGES_LOCK = threading.Lock()

BOT_MESSAGE_IDS = set()
BOT_MESSAGE_IDS_LOCK = threading.Lock()

def _mark_processed(message_id: str):
    if not message_id:
        return
    with PROCESSED_MESSAGES_LOCK:
        PROCESSED_MESSAGES.add(message_id)

def _is_processed(message_id: str) -> bool:
    if not message_id:
        return False
    with PROCESSED_MESSAGES_LOCK:
        return message_id in PROCESSED_MESSAGES

def _mark_bot_message(message_id: str):
    if not message_id:
        return
    with BOT_MESSAGE_IDS_LOCK:
        BOT_MESSAGE_IDS.add(message_id)

def _is_bot_message(message_id: str) -> bool:
    if not message_id:
        return False
    with BOT_MESSAGE_IDS_LOCK:
        return message_id in BOT_MESSAGE_IDS

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
    business_phone_number: Optional[str],
    background_tasks: BackgroundTasks,
) -> int:
    from external_api_service import ExternalApiService
    import requests
    import mimetypes

    handled_messages = 0
    messages = change.get("messages") or []
    print(f"Processing {len(messages)} incoming messages... incoming_phone_id={incoming_phone_id} business_phone_number={business_phone_number}")

    for idx, message in enumerate(messages):
        print(f"\n--- Handling message {idx+1}/{len(messages)} ---")
        user_phone = message.get("from")
        incoming_message_id = message.get("id")

        print(f"Message from={user_phone} id={incoming_message_id} raw_message={json.dumps(message)}")

        # Duplicate protection
        if _is_processed(incoming_message_id):
            print(f"Skipping already processed incoming id={incoming_message_id}")
            continue

        # Ignore webhooks that notify about outgoing messages from the business phone
        if business_phone_number and user_phone == business_phone_number:
            print(f"Ignoring webhook for outgoing business message from {business_phone_number}")
            _mark_processed(incoming_message_id)
            continue

        user_message, media_id, kind = _extract_user_message(message)
        print(f"Extracted user_message: {user_message}")
        print(f"Extracted media_id: {media_id}")
        print(f"Detected kind: {kind}")

        context = message.get("context") or {}
        context_id = context.get("id")
        forwarded_flag = bool(context.get("forwarded", False) or context.get("frequently_forwarded", False))
        print(f"context_id={context_id} forwarded={forwarded_flag}")

        # Ignore bot-like content reposted by users
        lowered = (user_message or "").lower()
        if any(phrase in lowered for phrase in ["msgflowx response", "strong match found", "buyer match #"]):
            print(f"Ignoring reposted bot content for incoming id={incoming_message_id} text={user_message}")
            _mark_processed(incoming_message_id)
            continue

        try:
            # Reply to a bot message -> call update_message_tag and stop processing (only if not forwarded)
            if context_id and not _is_bot_message(context_id):
                print(f"Incoming message is a reply to bot message: context_id={context_id} incoming_id={incoming_message_id}")
                try:
                    api_result = ExternalApiService.update_message_tag(message_id=context_id, tag=(user_message or ""))
                    print(f"update_message_tag result for context_id={context_id}: {api_result}")
                except Exception as exc:
                    print(f"Error calling update_message_tag: {exc}")
                    log_failure(source="external_api.update_message_tag", error=str(exc), phone=user_phone, message_id=incoming_message_id)
                _mark_processed(incoming_message_id)
                handled_messages += 1
                continue

            # Normal processing: forwarded messages fall through here as normal inquiries
            # Handle interactive button/list replies
            if message.get("type") == "interactive":
                # Route interactive actions (button_reply or list_reply)
                try:
                    logger.info("Received interactive incoming message: %s", message)
                    handle_project_button_click(message, user_phone, incoming_message_id)
                except Exception as exc:
                    logger.exception("Error handling interactive message: %s", exc)
                _mark_processed(incoming_message_id)
                handled_messages += 1
                continue

            if kind == "text" or kind is None:
                print(f"Calling ExternalApiService.send_text for incoming_id={incoming_message_id}")
                reply_text = ExternalApiService.send_text(user_message, incoming_message_id, user_phone)
                print(f"ExternalApiService.send_text returned: {reply_text}")

            elif kind in {"image"} and media_id:
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
                        ext = mimetypes.guess_extension(content_type) or (".jpg" if kind == "image" else ".bin")
                        filename = f"{media_id}{ext}"
                        reply_text = ExternalApiService.send_media(
                            media_bytes=media_bytes,
                            filename=filename,
                            mimetype=content_type,
                            message_id=incoming_message_id,
                            phone_number=user_phone,
                            raw_message=user_message,
                        )
                        print(f"ExternalApiService.send_media returned: {reply_text}")

            else:
                reply_text = "Sorry, this message type is not supported."

        except Exception as exc:
            log_failure(source="external_api", error=str(exc), phone=user_phone, message_id=incoming_message_id)
            reply_text = "Sorry, I am unable to process your request right now. Please try again later."

        # Build WhatsApp reply messages
        if reply_text is None:
            reply_messages = [{
                "phone_number": user_phone,
                "message_id": incoming_message_id,
                "text": (
                    "⚠️ Unable to process your request right now. "
                    "Please try again later."
                ),
            }]
        else:
            reply_messages = build_whatsapp_messages(reply_text)

        print(f"Generated reply_messages: {reply_messages}")

        # Send messages using background tasks
        from webhook_utils import send_message_async, send_message

        def _send_text_sync(user_phone_arg, text_arg, reply_to_id_arg=None):
            """Synchronous wrapper to send message using sync function."""
            try:
                resp = send_message(user_phone_arg, text_arg, reply_to_id_arg)
                print(f"send_message result for to={user_phone_arg} resp={resp}")
                if isinstance(resp, dict) and resp.get("message_id"):
                    mid = resp.get("message_id")
                    _mark_bot_message(mid)
                    _mark_processed(mid)
                    print(f"Stored outgoing bot message id={mid} and marked processed")
                else:
                    print(f"No outgoing message id returned or send failed for to={user_phone_arg} resp={resp}")
            except Exception as exc:
                print(f"Exception while sending message to {user_phone_arg}: {exc}")

        async def _send_project_message_bg(phone_arg, project_arg):
            """Background task to send project interactive message."""
            try:
                await send_project_message_async(phone_arg, project_arg)
            except Exception as exc:
                logger.exception("Error sending project message in background: %s", exc)

        for reply_message in reply_messages:

            # If this is a project match, send interactive message instead of text
            if reply_message.get("match_type") == "project":
                project = reply_message.get("project_data")
                logger.info("Project match detected for phone=%s project=%s", reply_message.get("phone_number"), project.get("project_name"))
                background_tasks.add_task(_send_project_message_bg, reply_message["phone_number"], project)
                continue

            target_phone = reply_message.get("phone_number") or user_phone
            target_text = reply_message.get("text")
            target_message_id = reply_message.get("message_id") or incoming_message_id
            print(f"Scheduling reply to {target_phone} message_id={target_message_id} text={target_text}")

            if reply_message.get("match_type") in {"broker_sell", "broker_buy"}:
                # Send broker matches immediately so they are not delayed or dropped by background job scheduling.
                _send_text_sync(target_phone, target_text, target_message_id)
            else:
                background_tasks.add_task(_send_text_sync, target_phone, target_text, target_message_id)

        # Mark processed to prevent duplicate processing
        _mark_processed(incoming_message_id)
        handled_messages += 1

    print(f"Total handled messages: {handled_messages}")
    return handled_messages

def is_forwarded_message(message: dict) -> bool:
    context = message.get("context", {})

    return (
        context.get("forwarded", False)
        or context.get("frequently_forwarded", False)
    )

from datetime import datetime, timezone

def build_whatsapp_messages(api_response):

    matches = api_response.get("matches", [])

    if not matches:
        return [{
            "phone_number": api_response.get("phone_number"),
            "message_id": api_response.get("message_id"),
            "text": api_response.get(
                "reply_message",
                "❌ No matching properties found."
            )
        }]

    messages = []

    for idx, match in enumerate(matches, start=1):

        match_type = match.get("match_type")

        target_phone = (
            match.get("phone_number")
            or api_response.get("phone_number")
        )

        target_message_id = (
            match.get("message_id")
            or api_response.get("message_id")
        )

        # -----------------------------------
        # PROJECT MATCH
        # -----------------------------------
        if match_type == "project":

            bedrooms = ", ".join(
                f"{b}BR"
                for b in match.get(
                    "bedrooms_available",
                    []
                )
            )

            msg = (
                f"🏗️ *New Project Match Found*\n\n"
                f"📌 {match.get('project_name')}\n"
                f"🏢 Developer: {match.get('developer')}\n"
                f"📍 Location: {match.get('area')}\n\n"
                f"💰 Starting Price: AED {int(match.get('starting_price', 0)):,}\n"
                f"🛏️ Available Units: {bedrooms}\n"
                f"💳 Payment Plan: {match.get('payment_plan') or 'N/A'}\n"
            )

            if match.get("handover"):
                msg += f"🏗️ Handover: {match.get('handover')}\n"

            if match.get("youtube_link"):
                msg += (
                    f"\n🎥 Video:\n"
                    f"{match.get('youtube_link')}"
                )

            if match.get("pdf_link"):
                msg += (
                    f"\n\n📄 Brochure:\n"
                    f"{match.get('pdf_link')}"
                )

        # -----------------------------------
        # BROKER SELL MATCH
        # -----------------------------------
        elif match_type == "broker_sell":
            print(f"Processing broker_sell match: {match}")
            property_info = match.get(
                "sell_snapshot",
                {}
            )

            broker = match.get(
                "sell_broker",
                {}
            )

            score = int(
                match.get("score", 0) * 100
            )

            msg = (
                f"🎯 *Property Match Found*\n\n"
                f"🏠 {property_info.get('bhk', 'N/A')} BR "
                f"{property_info.get('property_type', 'Property')}\n"
                f"📍 {property_info.get('location', 'N/A')}\n"
                f"💰 AED {property_info.get('price_aed', 0):,}\n\n"
                f"👤 Broker: {broker.get('name') or 'N/A'}\n"
                f"📞 {broker.get('phone') or 'N/A'}\n\n"
                f"⭐ Match Score: {score}%"
            )

        # -----------------------------------
        # BROKER BUY MATCH
        # -----------------------------------
        elif match_type == "broker_buy":
            print(f"Processing broker_buy match: {match}")
            buyer = match.get(
                "buy_snapshot",
                {}
            )

            broker = match.get(
                "buy_broker",
                {}
            )

            score = int(
                match.get("score", 0) * 100
            )

            budget = "N/A"

            if (
                buyer.get("price_min_aed")
                and buyer.get("price_max_aed")
            ):
                budget = (
                    f"AED {buyer['price_min_aed']:,}"
                    f" - "
                    f"AED {buyer['price_max_aed']:,}"
                )
            elif buyer.get("price_aed"):
                budget = (
                    f"AED {buyer['price_aed']:,}"
                )

            msg = (
                f"🎯 *Buyer Match Found*\n\n"
                f"🏠 Looking For: "
                f"{buyer.get('bhk', 'N/A')} BR "
                f"{buyer.get('property_type', 'Property')}\n"
                f"📍 Preferred Area: "
                f"{buyer.get('location', 'N/A')}\n"
                f"💰 Budget: {budget}\n\n"
                f"👤 Broker: "
                f"{broker.get('name') or 'N/A'}\n"
                f"📞 {broker.get('phone') or 'N/A'}\n\n"
                f"⭐ Match Score: {score}%"
            )

        else:
            print(f"Unknown match_type: {match_type} for match: {match}")
            msg = (
                api_response.get(
                    "reply_message",
                    "Match Found"
                )
            )
        print(f"Constructed message for match_type={match_type}: {msg}")
        # -----------------------------------
        # SAVE MATCH DETAILS
        # -----------------------------------

        try:

            message_doc = {
                "message_id":
                    target_message_id
                    or f"bot_match_{idx}_{int(datetime.now(timezone.utc).timestamp())}",
                "type": "bot_match",
                "timestamp":
                    datetime.now(timezone.utc).isoformat(),
                "text": msg,
                "match": match,
            }

            upsert_conversation_message(
                target_phone or "unknown",
                message_doc
            )

        except Exception as exc:
            print(
                f"Failed to persist match: {exc}"
            )

        messages.append({
            "phone_number": target_phone,
            "message_id": target_message_id,
            "text": msg,
            "match_type": match_type,
            "project_data": match
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
        logger.info("============================================== Received webhook Data ==============================================")
        # logger.info("Received webhook data: %s", json.dumps(data))
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
                business_phone_number = metadata.get("display_phone_number")
                print(f"Received change metadata: incoming_phone_id={incoming_phone_id} display_phone_number={business_phone_number} metadata={json.dumps(metadata)}")

                if _is_ignored_phone(incoming_phone_id) or change.get("messages") is None:
                    print(f"❌ Ignored webhook for phone_number_id: {incoming_phone_id}")
                    continue

                # Ignore status updates (sent, delivered, read)
                if "messages" not in change:
                    print("❌ Ignored non-message event")
                    continue
                # print(f"✅ Processing webhook for phone_number_id: {incoming_phone_id}")
                # print(f"Webhook change content: {json.dumps(change)}")
                handled_statuses += _log_status_events(change, incoming_phone_id)
                handled_messages += _process_incoming_messages(
                    change=change,
                    incoming_phone_id=incoming_phone_id,
                    business_phone_number=business_phone_number,
                    background_tasks=background_tasks,
                )

        if handled_messages == 0 and handled_statuses == 0:
            resp_content = {"status": "no_relevant_event"}
            # logger.info("Webhook processed but no relevant events; handled_messages=%d handled_statuses=%d response=%s",
            #     handled_messages,
            #     handled_statuses,
            #     resp_content,
            # )
            return JSONResponse(status_code=200, content=resp_content)

        resp_content = {"status": "ok", "handled_messages": handled_messages, "handled_statuses": handled_statuses}
        # logger.info("Webhook processed successfully; response=%s", resp_content)
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
    
async def send_project_message_async(phone, project):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, send_project_interactive_message, phone, project)
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from webhook_utils import llm_reply_to_text_v2
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)
app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
class WhatsAppMessage(BaseModel):
    object: str
    entry: list


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
        return JSONResponse(status_code=200, content={"status": "bad_request"})

    try:
        print("Received webhook data:", data)
        message_data = WhatsAppMessage(**data)

        if not message_data.entry:
            return JSONResponse(status_code=200, content={"status": "no entry"})

        changes = message_data.entry[0].get("changes") or []
        if not changes:
            return JSONResponse(status_code=200, content={"status": "no changes"})

        change = changes[0].get("value", {})
        # ✅ FILTER: Only allow your business number
        metadata = change.get("metadata", {})
        incoming_phone_id = metadata.get("phone_number_id")
        if incoming_phone_id != PHONE_NUMBER_ID:
            print(f"❌ Ignored webhook for phone_number_id: {incoming_phone_id}")
            return JSONResponse(status_code=200, content={"status": "ignored"})

        # ✅ Only process message events
        if 'messages' not in change:
            return JSONResponse(status_code=200, content={"status": "no message event"})

        print(f"Webhook change: {change}")
        if 'messages' in change:
            message = change["messages"][-1]
            user_phone = message["from"]
            print(message)
            if "text" in message:
                user_message = message["text"]["body"].lower()
                print(user_message)
                background_tasks.add_task(llm_reply_to_text_v2, user_message, user_phone, None, None)
            elif "image" in message:
                media_id = message["image"]["id"]
                print(media_id)
                caption = message["image"].get("caption", "")
                background_tasks.add_task(llm_reply_to_text_v2, caption, user_phone, media_id, "image")
            elif message.get("audio"):
                media_id = message["audio"]["id"]
                print(media_id)
                background_tasks.add_task(llm_reply_to_text_v2, "", user_phone, media_id, "audio")
            elif message.get("contacts"):
                contacts = message.get("contacts") or []
                if contacts:
                    contact_text = _contact_to_extraction_text(contacts[0])
                    background_tasks.add_task(llm_reply_to_text_v2, contact_text, user_phone, None, "contact")
        return JSONResponse(status_code=200, content={"status": "ok"})

    except Exception as exc:
        logger.exception("Unhandled error in webhook_handler: %r", exc)
        # Always return 200 so Meta does not retry the delivery.
        return JSONResponse(status_code=200, content={"status": "error"})
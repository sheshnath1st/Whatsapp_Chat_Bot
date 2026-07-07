import logging
import re
from typing import Dict, List, Tuple
from webhook_utils import _send_message_once, ACCESS_TOKEN
from conversation_store import upsert_conversation_message, log_event, log_failure, get_conversation
from external_api_service import ExternalApiService

logger = logging.getLogger(__name__)


def _get_project_id(project: dict) -> str:
    return str(project.get("project_id") or project.get("id") or re.sub(r"\s+", "_", (project.get("project_name") or "proj")).lower())


def build_project_buttons(project: dict) -> Tuple[str, List[dict]]:
    """
    Returns a tuple (mode, buttons) where mode is 'buttons' or 'list' and
    buttons is a list of reply/button dicts or list rows depending on mode.
    """
    pid = _get_project_id(project)

    buttons = []

    # Dynamic buttons
    if project.get("pdf_link"):
        buttons.append({"id": f"brochure_{pid}", "title": "📄 Brochure"})
    if project.get("youtube_link"):
        buttons.append({"id": f"video_{pid}", "title": "🎥 Video"})
    if project.get("image_link"):
        buttons.append({"id": f"gallery_{pid}", "title": "🖼 Gallery"})

    # Always present actions
    buttons.append({"id": f"interested_{pid}", "title": "👍 Interested"})
    buttons.append({"id": f"callback_{pid}", "title": "📞 Request Callback"})

    mode = "buttons" if len(buttons) <= 3 else "list"
    logger.info("Buttons generated for project %s: mode=%s buttons=%s", pid, mode, buttons)
    return mode, buttons


def _build_body_text(project: dict) -> str:
    bedrooms = " • ".join([f"{b}BR" for b in project.get("bedrooms_available", [])])
    score = int((project.get("score") or 0) * 100)
    body = (
        f"🏗️ New Project Recommendation\n\n"
        f"{project.get('project_name')}\n\n"
        f"📍 {project.get('area')}\n"
        f"🏢 Developer: {project.get('developer')}\n"
        f"💰 Starting AED {int(project.get('starting_price',0)):,}\n\n"
        f"🛏️ Available Units:\n{bedrooms if bedrooms else 'N/A'}\n\n"
        f"⭐ Match Score: {score}%\n\n"
        f"This project matches your requirement.\n\nSelect an option below."
    )
    return body


def send_project_interactive_message(phone: str, project: dict) -> dict:
    """Builds and sends the WhatsApp interactive message for a project."""
    mode, buttons = build_project_buttons(project)
    body_text = _build_body_text(project)

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
    }

    if mode == "buttons":
        payload["interactive"] = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons[:3]
                ]
            },
        }
    else:
        # Build list rows
        rows = [{"id": b["id"], "title": b["title"]} for b in buttons]
        payload["interactive"] = {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": "Select an option",
                "sections": [
                    {"title": "Options", "rows": rows}
                ]
            }
        }

    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    logger.info("Sending interactive project message for project=%s to=%s mode=%s", _get_project_id(project), phone, mode)
    resp = _send_message_once(payload, headers)

    # Save outgoing as conversation message
    try:
        message_doc = {
            "message_id": resp.get("message_id") or f"proj_msg_{_get_project_id(project)}",
            "type": "bot_match",
            "timestamp": resp.get("response_json", {}).get("timestamp") or None,
            "text": body_text,
            "match": project,
        }
        upsert_conversation_message(phone or "unknown", message_doc)
    except Exception:
        logger.exception("Failed to persist project interactive message meta")

    return resp


def handle_project_button_click(message: dict, user_phone: str, incoming_message_id: str):
    """Handle an incoming interactive click for project buttons."""
    try:
        interactive = message.get("interactive") or {}
        itype = interactive.get("type")
        if itype == "button":
            bid = interactive.get("button_reply", {}).get("id")
        elif itype == "list":
            bid = interactive.get("list_reply", {}).get("id")
        else:
            bid = None

        logger.info("Project button clicked: phone=%s incoming_id=%s button_id=%s", user_phone, incoming_message_id, bid)

        if not bid:
            logger.warning("No button id found in interactive message")
            return

        # Parse id
        if bid.startswith("brochure_"):
            pid = bid.split("brochure_")[-1]
            proj = message.get("project") or {}
            # If project not present in payload, try to resolve from context id in conversation history
            if not proj:
                ctx = (message.get("context") or {}).get("id")
                if ctx:
                    conv = get_conversation(user_phone) or {}
                    msgs = conv.get("messages") or []
                    for m in msgs:
                        if m.get("message_id") == ctx:
                            proj = m.get("match") or {}
                            break

            pdf = (proj or {}).get("pdf_link") or None
            if pdf:
                payload = {"messaging_product": "whatsapp", "to": user_phone, "type": "document", "document": {"link": pdf, "caption": "📄 Project Brochure"}}
                headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
                _send_message_once(payload, headers)
                logger.info("Brochure sent for project=%s to=%s", pid, user_phone)
                return
            logger.warning("Brochure requested but pdf link not available for project=%s", pid)

        elif bid.startswith("gallery_"):
            pid = bid.split("gallery_")[-1]
            proj = message.get("project") or {}
            if not proj:
                ctx = (message.get("context") or {}).get("id")
                if ctx:
                    conv = get_conversation(user_phone) or {}
                    msgs = conv.get("messages") or []
                    for m in msgs:
                        if m.get("message_id") == ctx:
                            proj = m.get("match") or {}
                            break
            img = (proj or {}).get("image_link") or None
            if img:
                payload = {"messaging_product": "whatsapp", "to": user_phone, "type": "image", "image": {"link": img, "caption": "🖼 Project Gallery"}}
                headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
                _send_message_once(payload, headers)
                logger.info("Gallery image sent for project=%s to=%s", pid, user_phone)
                return
            logger.warning("Gallery requested but image link not available for project=%s", pid)

        elif bid.startswith("video_"):
            pid = bid.split("video_")[-1]
            proj = message.get("project") or {}
            if not proj:
                ctx = (message.get("context") or {}).get("id")
                if ctx:
                    conv = get_conversation(user_phone) or {}
                    msgs = conv.get("messages") or []
                    for m in msgs:
                        if m.get("message_id") == ctx:
                            proj = m.get("match") or {}
                            break
            vid = (proj or {}).get("youtube_link") or None
            if vid:
                # send as text link
                from webhook_utils import send_message
                send_message(user_phone, f"🎥 Watch the project video:\n{vid}")
                logger.info("Video link sent for project=%s to=%s", pid, user_phone)
                return
            logger.warning("Video requested but link not available for project=%s", pid)

        elif bid.startswith("interested_"):
            pid = bid.split("interested_")[-1]
            # Use context id if present to update tag
            context_id = (message.get("context") or {}).get("id")
            if context_id:
                try:
                    ExternalApiService.update_message_tag(message_id=context_id, tag="INTERESTED")
                    logger.info("Marked INTERESTED for context_id=%s project=%s", context_id, pid)
                except Exception as exc:
                    logger.exception("Failed to update message tag for interested: %s", exc)
            # Persist in conversation history
            try:
                upsert_conversation_message(user_phone or "unknown", {"message_id": incoming_message_id, "type": "user_action", "timestamp": None, "text": f"INTERESTED on project {pid}"})
            except Exception:
                logger.exception("Failed to persist interest for project %s", pid)
            # Confirm to user
            from webhook_utils import send_message
            send_message(user_phone, "Thank you for your interest.\n\nOur property consultant will contact you shortly.")
            return

        elif bid.startswith("callback_"):
            pid = bid.split("callback_")[-1]
            context_id = (message.get("context") or {}).get("id")
            if context_id:
                try:
                    ExternalApiService.update_message_tag(message_id=context_id, tag="CALLBACK_REQUESTED")
                    logger.info("Marked CALLBACK_REQUESTED for context_id=%s project=%s", context_id, pid)
                except Exception as exc:
                    logger.exception("Failed to update message tag for callback: %s", exc)
            from webhook_utils import send_message
            send_message(user_phone, "Thanks — we've requested a callback. Our consultant will get in touch soon.")
            return

        else:
            logger.warning("Unhandled project button id=%s", bid)

    except Exception as exc:
        logger.exception("Error handling project button click: %s", exc)
        log_failure(source="project_interactive.handle_click", error=str(exc), phone=user_phone, message_id=incoming_message_id)
def build_project_buttons(project):

    buttons = []

    if project.get("pdf_link"):
        buttons.append({
            "id": f"brochure_{project['project_id']}",
            "title": "📄 Brochure"
        })

    if project.get("youtube_link"):
        buttons.append({
            "id": f"video_{project['project_id']}",
            "title": "🎥 Video"
        })

    if project.get("image_link"):
        buttons.append({
            "id": f"gallery_{project['project_id']}",
            "title": "🖼 Gallery"
        })

    buttons.append({
        "id": f"interested_{project['project_id']}",
        "title": "👍 Interested"
    })

    buttons.append({
        "id": f"callback_{project['project_id']}",
        "title": "📞 Callback"
    })

    return buttons



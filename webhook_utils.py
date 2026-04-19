import os
import asyncio
import tempfile
import json
import requests
import httpx
from dotenv import load_dotenv
from typing import Optional
from conversation_store import log_event, log_failure

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
        if isinstance(response_body, dict):
            salesforce_id = (
                response_body.get("id")
                or response_body.get("Id")
                or response_body.get("recordId")
                or response_body.get("salesforce_id")
                or response_body.get("sf_id")
            )
        
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


def _salesforce_status_message(salesforce_result: Optional[dict]) -> str:
    if not salesforce_result:
        return ""
    if salesforce_result.get("ok"):
        sf_id = salesforce_result.get("salesforce_id")
        if sf_id:
            return f"Salesforce updated successfully. Record ID: {sf_id}"
        return "Salesforce updated successfully."
    return "Salesforce update failed."


def _to_salesforce_payload(extracted_data: dict) -> Optional[dict]:
    """Map normalized extraction output to legacy Salesforce contact fields."""
    if not isinstance(extracted_data, dict):
        return None

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
    return payload


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






async def llm_reply_to_text_v2(
    user_input: str,
    user_phone: str,
    media_id: str = None,
    kind: str = None,
    incoming_message_id: str = None,
):
    try:
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }

        json_data = {
            "user_input": user_input,
            "media_id": media_id,
            "kind": kind,
        }
        print(" LLM API request:", json_data)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{AGENT_URL}/llm-response",
                json=json_data,
                headers=headers,
                timeout=120,
            )

        if response.status_code == 200 and response.headers.get("content-type", "").startswith("audio"):
            # Audio payload (kind == "audio")
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
                    payload={
                        "kind": "audio",
                        "whatsapp_send_result": send_result,
                    },
                )
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return

        response_data = response.json()
        print("LLM API response:", response_data, "of type", type(user_phone))
        if response.status_code == 200 and response_data.get("error") is None:
            message_content = response_data.get("response")
            salesforce_payload = None
            salesforce_result = None
            
            # Check if response contains business card data (from audio/image/contact extraction)
            if isinstance(message_content, dict):
                salesforce_payload = _to_salesforce_payload(message_content)
                message_content = json.dumps(message_content, ensure_ascii=False)

            if salesforce_payload:
                loop = asyncio.get_running_loop()
                salesforce_result = await loop.run_in_executor(None, send_to_salesforce, salesforce_payload)
                sf_status = _salesforce_status_message(salesforce_result)
                if sf_status:
                    message_content = f"{message_content}\n\n{sf_status}" if message_content else sf_status
            
            print(f"LLM API message content: {message_content}")
            if message_content:
                loop = asyncio.get_running_loop()
                print(f"Sending message to {user_phone}: {message_content}")
                send_result = await loop.run_in_executor(None, send_message, user_phone, message_content)
                if not (send_result or {}).get("ok"):
                    log_failure(
                        source="llm_reply_to_text_v2.send_message_failed",
                        error=(send_result or {}).get("error", "whatsapp_send_failed"),
                        phone=user_phone,
                        related_message_id=incoming_message_id,
                        payload={
                            "reply": message_content,
                            "send_result": send_result,
                        },
                    )
                log_event(
                    event_type="outgoing_message",
                    direction="outgoing",
                    phone=user_phone,
                    message_id=(send_result or {}).get("message_id"),
                    related_message_id=incoming_message_id,
                    payload={
                        "kind": kind or "text",
                        "reply": message_content,
                        "salesforce_payload": salesforce_payload,
                        "salesforce_result": salesforce_result,
                        "whatsapp_send_result": send_result,
                    },
                )

                if salesforce_result:
                    log_event(
                        event_type="salesforce_sync",
                        direction="system",
                        phone=user_phone,
                        related_message_id=incoming_message_id,
                        payload={
                            "salesforce_payload": salesforce_payload,
                            "salesforce_result": salesforce_result,
                        },
                    )
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
                payload={
                    "status_code": response.status_code,
                    "response_data": response_data,
                },
            )
            await send_message_async(user_phone, "Failed to process message due to an internal server error.")

    except Exception as e:
        print("LLM error:", e)
        log_failure(
            source="llm_reply_to_text_v2.exception",
            error=str(e),
            phone=user_phone,
            related_message_id=incoming_message_id,
            payload={
                "user_input": user_input,
                "media_id": media_id,
                "kind": kind,
            },
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
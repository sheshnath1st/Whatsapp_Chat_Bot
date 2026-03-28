import os
import asyncio
import tempfile
import json
import requests
import httpx
from dotenv import load_dotenv

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

def send_message(to: str, text: str):
    print(f"Preparing to send message to {to}: {text}")
    if not text:
        print("Error: Message text is empty.")
        return

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    print(f"Send URL WHATSAPP_API_URL: {WHATSAPP_API_URL}")
    print(f"Send URL headers: {headers}")
    print(f"Send URL payload: {payload}")

    for attempt in range(3):
        try:
            response = requests.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=15)
            if response.status_code == 200:
                print("Message sent")
                return
            print(f"Send failed (attempt {attempt + 1}): {response.text}")
        except requests.exceptions.Timeout:
            print(f"Send timeout (attempt {attempt + 1})")
        except Exception as exc:
            print(f"Send error (attempt {attempt + 1}): {exc}")
            break  # Non-transient error; don't retry



async def send_message_async(user_phone: str, message: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_message, user_phone, message)



        
async def send_audio_message(to: str, file_path: str):
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
            "Content-Type": "application/json"
        }
        requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
    else:
        print("Audio upload failed:", response.text)






async def llm_reply_to_text_v2(user_input: str, user_phone: str, media_id: str = None, kind: str = None):
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
                await send_audio_message(user_phone, temp_path)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return

        response_data = response.json()
        print("LLM API response:", response_data)
        if response.status_code == 200 and response_data.get("error") is None:
            message_content = response_data.get("response")
            if isinstance(message_content, dict):
                message_content = json.dumps(message_content, ensure_ascii=False)
            print(f"LLM API message content: {message_content}")
            if message_content:
                loop = asyncio.get_running_loop()
                print(f"Sending message to {user_phone}: {message_content}")
                await loop.run_in_executor(None, send_message, user_phone, message_content)
            else:
                print("Error: Empty message content from LLM API")
                await send_message_async(user_phone, "Received empty response from LLM API.")
        else:
            print("Error: Invalid LLM API response", response_data)
            await send_message_async(user_phone, "Failed to process message due to an internal server error.")

    except Exception as e:
        print("LLM error:", e)
        try:
            await send_message_async(user_phone, "Sorry, something went wrong while generating a response.")
        except Exception as send_err:
            print("Failed to send error reply:", send_err)
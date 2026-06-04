import os
import requests
import tempfile
import logging

API_URL = os.getenv("EXTERNAL_API_URL", "https://precision-fifth-headband.ngrok-free.dev/ingest/whatsapp")
API_KEY = os.getenv("EXTERNAL_API_KEY", "your_api_key_here")
logger = logging.getLogger(__name__)

class ExternalApiService:
    @staticmethod
    def send_text(text, message_id, phone_number):
        payload = {
            # "message_type": "text",
            "raw_message": text,
            "message_id": message_id,
            "phone_number": phone_number
        }
        headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
        try:
            logger.info(f"Sending text to API: {payload}")
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
            logger.info(f"API response: {resp.status_code} {resp.text}")
            if resp.status_code == 200:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"reply": resp.text}
                return data.get("reply") or resp.text
            else:
                logger.error(f"API error: {resp.status_code} {resp.text}")
                return None
        except Exception as exc:
            logger.exception(f"Failed to call API: {exc}")
            return None

    @staticmethod
    def send_media(media_bytes, filename, mimetype, message_id, phone_number, raw_message=""):
        files = {
            "file": (filename, media_bytes, mimetype)
        }

        data = {
            "message_id": message_id,
            "phone_number": phone_number,
            "raw_message": raw_message
        }

        headers = {
            "X-API-Key": API_KEY
        }

        try:
            logger.info(
                f"Sending media to API: "
                f"filename={filename}, "
                f"mimetype={mimetype}, "
                f"message_id={message_id}, "
                f"phone_number={phone_number}"
            )

            resp = requests.post(
                API_URL,
                headers=headers,
                data=data,
                files=files,
                timeout=60
            )

            logger.info(f"API response: {resp.status_code}")
            logger.info(f"API response body: {resp.text}")

            if resp.status_code == 200:
                api_data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {"reply_message": resp.text}
                )

                return api_data

            logger.error(f"API error: {resp.status_code} {resp.text}")
            return None

        except Exception as exc:
            logger.exception(f"Failed to call API for media: {exc}")
            return None
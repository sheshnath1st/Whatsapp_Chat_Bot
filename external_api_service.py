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
        files = {
            "message_id": (None, str(message_id)),
            "phone_number": (None, str(phone_number)),
            "raw_message": (None, text)
        }

        headers = {
            "X-API-Key": API_KEY
        }

        try:
            logger.info(
                f"Sending text to API: "
                f"message_id={message_id}, "
                f"phone_number={phone_number}"
            )

            resp = requests.post(
                API_URL,
                headers=headers,
                files=files,
                timeout=30
            )

            logger.info(f"API response: {resp.status_code}")
            logger.info(f"API response body: {resp.text}")

            if resp.status_code == 200:
                api_data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {"reply_message": resp.text}
                )

                return api_data.get("reply_message") or resp.text

            logger.error(f"API error: {resp.status_code} {resp.text}")
            return None

        except Exception as exc:
            logger.exception(f"Failed to call API: {exc}")
            return None

    @staticmethod
    def send_media(media_bytes, filename, mimetype, message_id, phone_number, raw_message=""):

        files = {
            "message_id": (None, str(message_id)),
            "phone_number": (None, str(phone_number)),
            "raw_message": (None, raw_message),
            "file": (filename, media_bytes, mimetype)
        }

        headers = {
            "X-API-Key": API_KEY
        }

        try:
            logger.info(
                f"Sending media: "
                f"message_id={message_id}, "
                f"phone_number={phone_number}, "
                f"filename={filename}"
            )

            resp = requests.post(
                API_URL,
                headers=headers,
                files=files,
                timeout=60
            )

            logger.info(f"Status: {resp.status_code}")
            logger.info(f"Response: {resp.text}")

            if resp.status_code == 200:
                return resp.json()

            return None

        except Exception as exc:
            logger.exception(f"Failed to call API: {exc}")
            return None
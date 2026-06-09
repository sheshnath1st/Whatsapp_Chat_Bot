import os
import requests
import tempfile
import logging

API_URL = os.getenv("EXTERNAL_API_URL", "https://precision-fifth-headband.ngrok-free.dev/ingest/whatsapp")
UPDATE_API_URL = os.getenv("UPDATE_API_URL", "https://precision-fifth-headband.ngrok-free.dev/ingest/update")
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
                f"phone_number={phone_number}, "
                f"raw_message={text}"
            )

            resp = requests.post(
                API_URL,
                headers=headers,
                files=files,
                timeout=120
            )

            logger.info(f"API response: {resp.status_code}")
            logger.info(f"API response body: {resp.text}")

            if resp.status_code == 200:
                api_data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {"reply_message": resp.text}
                )

                return api_data or resp.text

            logger.error(f"API error: {resp.status_code} {resp.text}")
            return None

        except Exception as exc:
            logger.exception(f"Failed to call API: {exc}")
            return None

    @staticmethod
    def update_message_tag(message_id, tag):

        files = {
            "message_id": (None, str(message_id)),
            "tag": (None, str(tag))
        }

        headers = {
            "X-API-Key": API_KEY
        }

        try:
            logger.info(
                f"Updating message tag: "
                f"message_id={message_id}, "
                f"tag={tag}"
            )

            resp = requests.post(
                UPDATE_API_URL,
                headers=headers,
                files=files,
                timeout=120
            )

            logger.info(f"Update status: {resp.status_code}")
            logger.info(f"Update response: {resp.text}")

            if resp.status_code == 200:
                return resp.json()

            logger.error(
                f"Update API error: "
                f"{resp.status_code} {resp.text}"
            )

            return None

        except Exception as exc:
            logger.exception(
                f"Failed to update message tag: {exc}"
            )
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
                f"filename={filename}, "
                f"raw_message={raw_message}"
            )

            resp = requests.post(
                API_URL,
                headers=headers,
                files=files,
                timeout=120
            )

            logger.info(f"Status: {resp.status_code}")
            logger.info(f"Response: {resp.text}")

            if resp.status_code == 200:
                return resp.json()

            return None

        except Exception as exc:
            logger.exception(f"Failed to call API: {exc}")
            return None
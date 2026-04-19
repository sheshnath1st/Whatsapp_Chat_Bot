import io
import importlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import requests
from dotenv import load_dotenv

from conversation_store import log_event, log_failure

load_dotenv()

logger = logging.getLogger(__name__)

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_PREFIX = os.getenv("S3_PREFIX", "whatsapp-media")
AWS_REGION = os.getenv("AWS_REGION")

_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
}

_AUDIO_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
}


def _normalize_content_type(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _detect_extension(kind: str, content_type: Optional[str]) -> str:
    normalized = _normalize_content_type(content_type)
    if kind == "image":
        return _IMAGE_EXTENSIONS.get(normalized, "jpg")
    if kind == "audio":
        return _AUDIO_EXTENSIONS.get(normalized, "ogg")
    return "bin"


def _build_s3_key(kind: str, ext: str, incoming_message_id: Optional[str]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    message_part = incoming_message_id or "no_message_id"
    unique_id = uuid4().hex
    return f"{S3_PREFIX}/{kind}/{timestamp}/{message_part}_{unique_id}.{ext}"


def _fetch_media_download_url(media_id: str) -> Optional[str]:
    if not ACCESS_TOKEN:
        logger.error("ACCESS_TOKEN is not configured; cannot fetch WhatsApp media URL")
        return None

    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{media_id}"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        timeout=20,
    )
    if response.status_code != 200:
        logger.error("Failed to fetch media URL for media_id=%s status=%s", media_id, response.status_code)
        return None
    return (response.json() or {}).get("url")


def _download_media_bytes(media_url: str) -> tuple[Optional[bytes], Optional[str]]:
    response = requests.get(
        media_url,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        timeout=30,
    )
    if response.status_code != 200:
        logger.error("Failed to download media content from WhatsApp, status=%s", response.status_code)
        return None, None
    return response.content, _normalize_content_type(response.headers.get("content-type"))


def _s3_client():
    boto3 = importlib.import_module("boto3")
    kwargs = {}
    if AWS_REGION:
        kwargs["region_name"] = AWS_REGION
    return boto3.client("s3", **kwargs)


def upload_whatsapp_media_to_s3(
    media_id: str,
    kind: str,
    incoming_message_id: Optional[str] = None,
    user_phone: Optional[str] = None,
) -> Optional[dict]:
    """Download WhatsApp media by media_id and store it in S3."""
    if kind not in {"image", "audio"}:
        return None

    if not S3_BUCKET_NAME:
        log_failure(
            source="upload_whatsapp_media_to_s3.not_configured",
            error="S3_BUCKET_NAME not configured",
            phone=user_phone,
            message_id=incoming_message_id,
            payload={"media_id": media_id, "kind": kind},
        )
        return None

    if not ACCESS_TOKEN:
        log_failure(
            source="upload_whatsapp_media_to_s3.not_configured",
            error="ACCESS_TOKEN not configured",
            phone=user_phone,
            message_id=incoming_message_id,
            payload={"media_id": media_id, "kind": kind},
        )
        return None

    try:
        media_url = _fetch_media_download_url(media_id)
        if not media_url:
            log_failure(
                source="upload_whatsapp_media_to_s3.fetch_url_failed",
                error="Unable to fetch WhatsApp media URL",
                phone=user_phone,
                message_id=incoming_message_id,
                payload={"media_id": media_id, "kind": kind},
            )
            return None

        media_bytes, content_type = _download_media_bytes(media_url)
        if not media_bytes:
            log_failure(
                source="upload_whatsapp_media_to_s3.download_failed",
                error="Unable to download WhatsApp media bytes",
                phone=user_phone,
                message_id=incoming_message_id,
                payload={"media_id": media_id, "kind": kind, "media_url": media_url},
            )
            return None

        file_ext = _detect_extension(kind, content_type)
        s3_key = _build_s3_key(kind, file_ext, incoming_message_id)
        extra_args = {"ContentType": content_type} if content_type else {}

        client = _s3_client()
        client.upload_fileobj(
            io.BytesIO(media_bytes),
            S3_BUCKET_NAME,
            s3_key,
            ExtraArgs=extra_args,
        )

        result = {
            "bucket": S3_BUCKET_NAME,
            "key": s3_key,
            "content_type": content_type,
            "size_bytes": len(media_bytes),
            "kind": kind,
            "media_id": media_id,
        }
        log_event(
            event_type="incoming_media_uploaded",
            direction="incoming",
            phone=user_phone,
            message_id=incoming_message_id,
            payload=result,
        )
        return result

    except Exception as exc:
        logger.exception("Failed to upload media_id=%s kind=%s to S3", media_id, kind)
        log_failure(
            source="upload_whatsapp_media_to_s3.exception",
            error=str(exc),
            phone=user_phone,
            message_id=incoming_message_id,
            payload={"media_id": media_id, "kind": kind},
        )
        return None
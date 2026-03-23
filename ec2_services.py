from openai import OpenAI
import os
import base64
import json
import re
import logging
import requests
import httpx
import tempfile
from PIL import Image
from dotenv import load_dotenv
from io import BytesIO
from pathlib import Path
from typing import Optional
from groq import Groq

try:
    import whisper
except Exception:
    whisper = None

load_dotenv()

logger = logging.getLogger(__name__)

TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY")
LLAMA_API_KEY = os.getenv("LLAMA_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL")
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "groq").lower()
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
GROQ_MODEL = os.getenv("GROQ_MODEL") or "llama-3.1-8b-instant"
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL") or "gpt-4o-mini-transcribe"
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL") or "whisper-large-v3-turbo"
BUSINESS_CARD_PYTHON_FIRST = (os.getenv("BUSINESS_CARD_PYTHON_FIRST") or "true").lower() == "true"
BUSINESS_CARD_MIN_FIELDS = int(os.getenv("BUSINESS_CARD_MIN_FIELDS") or 3)

BUSINESS_CARD_SCHEMA = {
    "name": None,
    "designation": None,
    "company": None,
    "phone": None,
    "email": None,
    "website": None,
    "address": None,
}


def _normalize_business_card_schema(data: dict) -> dict:
    """Normalize extracted output into a fixed schema with nullable fields."""
    if not isinstance(data, dict):
        return dict(BUSINESS_CARD_SCHEMA)

    normalized = {}
    for key in BUSINESS_CARD_SCHEMA:
        value = data.get(key)
        if isinstance(value, str):
            value = value.strip() or None
        normalized[key] = value if value is not None else None
    return normalized


def safe_json_parse(response: str) -> dict | None:
    """
    Parse JSON from model output safely.

    Strategy:
    1. direct json.loads
    2. fenced code block extraction
    3. first JSON object regex fallback
    """
    if not response:
        return None

    text = response.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fenced_match:
        try:
            parsed = json.loads(fenced_match.group(1))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

    object_match = re.search(r"\{[\s\S]*\}", text)
    if object_match:
        try:
            parsed = json.loads(object_match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    return None


def _call_text_llm(messages: list[dict]) -> str | None:
    """Call configured text LLM provider (Groq/OpenAI) with chat messages."""
    try:
        if LLM_PROVIDER == "openai":
            client = OpenAI(api_key=OPENAI_API_KEY)
            completion = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
            )
        else:
            client = Groq(api_key=GROQ_API_KEY)
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
            )

        if completion.choices and len(completion.choices) > 0:
            return completion.choices[0].message.content
        return None
    except Exception as e:
        logger.exception("Text LLM call failed: %s", e)
        return None


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,5}\)?[\s-]?)?\d{3,5}[\s-]?\d{3,6}")
_WEB_RE = re.compile(r"(?:https?://)?(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/\S*)?")
_DESIGNATION_HINTS = (
    "manager",
    "director",
    "engineer",
    "developer",
    "consultant",
    "architect",
    "officer",
    "founder",
    "ceo",
    "cto",
    "coo",
    "president",
    "lead",
    "head",
    "sales",
    "marketing",
)
_COMPANY_HINTS = ("ltd", "limited", "pvt", "inc", "llp", "technologies", "solutions", "corp", "company")


def _populated_field_count(data: dict) -> int:
    return sum(1 for value in data.values() if value not in (None, ""))


def _python_extract_from_text(text: str) -> dict:
    """Heuristic field extraction from OCR/transcribed text without calling an LLM."""
    if not text:
        return dict(BUSINESS_CARD_SCHEMA)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return dict(BUSINESS_CARD_SCHEMA)

    emails = _EMAIL_RE.findall(text)
    websites = [w for w in _WEB_RE.findall(text) if "." in w and "@" not in w]
    phones = _PHONE_RE.findall(text)

    designation = next((l for l in lines if any(h in l.lower() for h in _DESIGNATION_HINTS)), None)
    company = next((l for l in lines if any(h in l.lower() for h in _COMPANY_HINTS)), None)

    name = None
    for line in lines[:6]:
        # Prefer short alphabetic top lines that do not look like contact info.
        if any(token in line.lower() for token in ("@", "www", "http", "+", "tel", "phone")):
            continue
        if len(line.split()) <= 4 and re.search(r"[A-Za-z]", line):
            name = line
            break

    address_lines = [
        l for l in lines
        if any(k in l.lower() for k in ("road", "rd", "street", "st", "avenue", "ave", "floor", "near", "city", "state", "india", "pin"))
    ]

    extracted = {
        "name": name,
        "designation": designation,
        "company": company,
        "phone": ", ".join(dict.fromkeys(p.strip() for p in phones if len(re.sub(r"\D", "", p)) >= 8)) or None,
        "email": emails[0] if emails else None,
        "website": websites[0] if websites else None,
        "address": ", ".join(address_lines) if address_lines else None,
    }
    return _normalize_business_card_schema(extracted)


def _python_extract_from_image(image_base64: str) -> dict | None:
    """Run local OCR + regex extraction. Returns None when OCR is unavailable/fails."""
    if not image_base64:
        return None

    try:
        import pytesseract
    except Exception:
        logger.info("Python-first OCR skipped: pytesseract is not installed.")
        return None

    try:
        # Accept both raw base64 and data URL input.
        image_payload = image_base64.split(",", 1)[-1] if "base64," in image_base64 else image_base64
        image_bytes = base64.b64decode(image_payload)
        image = Image.open(BytesIO(image_bytes)).convert("L")
        ocr_text = pytesseract.image_to_string(image)
        logger.info("Python OCR text (first 400 chars): %s", ocr_text[:400])
        return _python_extract_from_text(ocr_text)
    except Exception as e:
        logger.exception("Python-first OCR extraction failed: %s", e)
        return None


def extract_business_card_details(input_text: str = None, image_base64: str = None) -> dict | None:
    """
    Extract structured business card fields from text or image.

    Returns a normalized dict with fixed keys, or None when extraction fails.
    """
    if not input_text and not image_base64:
        logger.error("extract_business_card_details called without text or image")
        return None

    system_prompt = (
        "You are an information extraction engine for business cards. "
        "Extract only these fields: name, designation, company, phone, email, website, address. "
        "Return ONLY valid JSON object with exactly these keys. "
        "If a field is unavailable, use null. Do not add explanations or markdown."
    )

    raw_response = None
    try:
        if image_base64:
            logger.info("Business card extraction input type: image")
            if BUSINESS_CARD_PYTHON_FIRST:
                python_result = _python_extract_from_image(image_base64)
                if python_result:
                    populated = _populated_field_count(python_result)
                    logger.info(
                        "Python-first image extraction fields=%d threshold=%d payload=%s",
                        populated,
                        BUSINESS_CARD_MIN_FIELDS,
                        python_result,
                    )
                    if populated >= BUSINESS_CARD_MIN_FIELDS:
                        return python_result

                logger.info("Python-first image extraction insufficient, falling back to AI vision")

            # Vision extraction uses OpenAI because current Groq path in this project is text-only.
            client = OpenAI(api_key=OPENAI_API_KEY)
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract business card details and return only valid JSON.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                    ],
                },
            ]
            completion = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
            )
            if completion.choices and len(completion.choices) > 0:
                raw_response = completion.choices[0].message.content
        else:
            logger.info("Business card extraction input type: audio-text")
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Text from spoken business card:\n{input_text}",
                },
            ]
            raw_response = _call_text_llm(messages)

        logger.info("Business card LLM raw response: %s", raw_response)
        parsed = safe_json_parse(raw_response)
        logger.info("Business card parsed JSON: %s", parsed)

        if not parsed:
            return None
        return _normalize_business_card_schema(parsed)
    except Exception as e:
        logger.exception("Business card extraction failed: %s", e)
        return None

def text_to_speech(text: str, output_path: str = "reply.mp3") -> Optional[str]:
    """
    Synthesizes a given text into an audio file using OpenAI's TTS service.

    Args:
        text (str): The text to be synthesized.
        output_path (str): The path where the output audio file will be saved. Defaults to "reply.mp3".

    Returns:
        str: The path to the output audio file, or None if the synthesis failed.
    """
    try:
        client = OpenAI()
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice="alloy",
            input=text
        )
        
        # Convert string path to Path object and stream the response to a file
        path_obj = Path(output_path)
        response.write_to_file(path_obj)
        return str(path_obj)
    except Exception as e:
        print(f"TTS failed: {e}")
        return None


def speech_to_text(input_path: str) -> str:
    """
    Speech-to-text with ordered fallback:
    1. Local Whisper (best control)
    2. OpenAI transcription (high accuracy)
    3. Groq transcription (fast fallback)
    """
    local_model_name = os.getenv("LOCAL_WHISPER_MODEL", "base")

    # 1) Local Whisper
    try:
        if whisper is None:
            raise ImportError("whisper package is not installed")

        logger.info("[STT] Using Local Whisper model=%s", local_model_name)
        model = whisper.load_model(local_model_name)
        result = model.transcribe(
            input_path,
            fp16=False,
            language=None,  # auto-detect language
        )
        text = (result.get("text") or "").strip()
        if text:
            logger.info("[STT] Local Whisper success: %s", text)
            return text
    except Exception as e:
        logger.warning("[STT] Local Whisper failed: %r", e)

    # 2) OpenAI transcription
    try:
        logger.info("[STT] Trying OpenAI transcription")
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(input_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=OPENAI_STT_MODEL,
                file=audio_file,
            )
        text = (response.text or "").strip()
        if text:
            logger.info("[STT] OpenAI success: %s", text)
            return text
    except Exception as e:
        logger.warning("[STT] OpenAI failed: %r", e)

    # 3) Groq fallback
    try:
        logger.info("[STT] Falling back to Groq transcription")
        client = Groq(api_key=GROQ_API_KEY)
        with open(input_path, "rb") as file:
            transcription = client.audio.transcriptions.create(
                model=GROQ_STT_MODEL,
                response_format="verbose_json",
                file=(input_path, file.read()),
            )
        text = (transcription.text or "").strip()
        if text:
            logger.info("[STT] Groq success: %s", text)
            return text
    except Exception as e:
        logger.error("[STT] All STT methods failed: %r", e)

    return ""
      




def get_llm_response(text_input: str, image_input : str = None) -> Optional[str]:
    """
    Get the response from the LLM given a text input and an optional image input.

    Args:
        text_input (str): The text to be sent to the LLM.
        image_input (str, optional): The base64 encoded image to be sent to the LLM. Defaults to None.

    Returns:
        str: The response from the LLM.
    """
    try:
        if image_input:
            # Use OpenAI GPT-4 Vision for image processing
            client = OpenAI(api_key=OPENAI_API_KEY)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": text_input
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_input}"
                            }
                        }
                    ]
                }
            ]
            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )
        else:
            # Use Groq for text-only responses
            client = Groq(api_key=GROQ_API_KEY)
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {
                        "role": "user",
                        "content": text_input
                    }
                ]
            )
        
        if completion.choices and len(completion.choices) > 0:
            return completion.choices[0].message.content
        else:
            print("Empty response from LLM API")
            return None
    except Exception as e:
        print(f"LLM error: {e}")
        return None







async def fetch_media(media_id: str) -> Optional[str]:
    """
    Fetches the URL of a media given its ID.

    Args:
        media_id (str): The ID of the media to fetch.

    Returns:
        str: The URL of the media.
    """
    url = "https://graph.facebook.com/v22.0/{media_id}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                url.format(media_id=media_id),
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
            )
            if response.status_code == 200:
                return response.json().get("url")
            else:
                print(f"Failed to fetch media: {response.text}")
        except Exception as e:
            print(f"Exception during media fetch: {e}")
    return None

async def handle_image_message(media_id: str) -> str:
    """
    Handle an image message by fetching the image media, converting it to base64,
    and returning the base64 string.

    Args:
        media_id (str): The ID of the image media to fetch.

    Returns:
        str: The base64 string of the image.
    """
    media_url = await fetch_media(media_id)
    # print(media_url)
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        response = await client.get(media_url, headers=headers)
        response.raise_for_status()

        # Convert image to base64
        image = Image.open(BytesIO(response.content))
        buffered = BytesIO()
        image.save(buffered, format="JPEG")  # Save as JPEG
        # image.save("./test.jpeg", format="JPEG")  # Optional save
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        return base64_image

async def handle_audio_message(media_id: str):
    """
    Handle an audio message by fetching the audio media, writing it to a temporary file,
    and then using Groq to transcribe the audio to text.

    Args:
        media_id (str): The ID of the audio media to fetch.

    Returns:
        str: The transcribed text.
    """
    media_url = await fetch_media(media_id)
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        response = await client.get(media_url, headers=headers)

        response.raise_for_status()
        audio_bytes = response.content
        content_type = (response.headers.get("content-type") or "").lower()
        suffix_map = {
            "audio/ogg": ".ogg",
            "audio/opus": ".opus",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/webm": ".webm",
        }
        suffix = ".ogg"
        for mime, ext in suffix_map.items():
            if mime in content_type:
                suffix = ext
                break

        logger.info("Audio download content-type=%s size=%d suffix=%s", content_type, len(audio_bytes), suffix)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(audio_bytes)
            temp_audio_path = temp_file.name

        try:
            transcription = speech_to_text(temp_audio_path)
            logger.info("Transcription text length=%d", len(transcription or ""))
            return transcription
        finally:
            try:
                os.remove(temp_audio_path)
            except OSError:
                pass

async def send_audio_message(to: str, file_path: str):
    """
    Send an audio message to a WhatsApp user.

    Args:
        to (str): The phone number of the recipient.
        file_path (str): The path to the audio file to be sent.

    Returns:
        None

    Raises:
        None
    """
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
    with open(file_path, "rb") as f:
        files = {"file": ("reply.mp3", f, "audio/mpeg")}
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
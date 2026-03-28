from __future__ import annotations

from openai import OpenAI
import os
import base64
import json
import re
import logging
import requests
import httpx
import tempfile
import subprocess
import shutil
import gc
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
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL") or "gpt-4o-transcribe"
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL") or "whisper-large-v3-turbo"
STT_LANGUAGE_HINT = (os.getenv("STT_LANGUAGE_HINT") or "").strip() or None
STT_PROMPT = (os.getenv("STT_PROMPT") or "").strip() or None
BUSINESS_CARD_PYTHON_FIRST = (os.getenv("BUSINESS_CARD_PYTHON_FIRST") or "true").lower() == "true"
BUSINESS_CARD_MIN_FIELDS = int(os.getenv("BUSINESS_CARD_MIN_FIELDS") or 3)
LLM_FALLBACK_MIN_FIELDS = 3
SUCCESS_MIN_FIELDS = 2

# Low-memory EC2 tuning: use smallest Whisper models by default; resize large images.
# Override via env vars, e.g. WHISPER_MODELS=small,medium for a more capable instance.
EC2_WHISPER_MODELS = [
    m.strip()
    for m in (os.getenv("WHISPER_MODELS") or "base,tiny").split(",")
    if m.strip()
]
MAX_IMAGE_DIMENSION = int(os.getenv("MAX_IMAGE_DIMENSION") or 1024)
# Cache the loaded Whisper model to avoid reloading on every audio request.
WHISPER_CACHE_MODEL = (os.getenv("WHISPER_CACHE_MODEL") or "true").lower() == "true"
_whisper_model_cache: dict = {}

BUSINESS_CARD_SCHEMA = {
    "name": None,
    "designation": None,
    "company": None,
    "phone": None,
    "email": None,
    "website": None,
    "address": None,
}


_NULL_LIKE_VALUES = {"", "null", "none", "n/a", "na", "nil", "unknown", "not available"}


def _clean_field_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.lower() in _NULL_LIKE_VALUES:
            return None
        return cleaned or None
    return value


def normalize_output(data: dict) -> dict:
    """Return strict schema output with cleaned values."""
    if not isinstance(data, dict):
        return dict(BUSINESS_CARD_SCHEMA)

    normalized = {}
    for key in BUSINESS_CARD_SCHEMA:
        normalized[key] = _clean_field_value(data.get(key))
    return normalized


def merge_results(primary: dict, fallback: dict) -> dict:
    """Merge two normalized card dicts, preferring non-null values from primary."""
    merged = {}
    for key in BUSINESS_CARD_SCHEMA:
        merged[key] = primary.get(key) if primary.get(key) is not None else fallback.get(key)
    return normalize_output(merged)


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
# Matches strict 10-digit Indian numbers (+91 prefix optional)
_INDIAN_PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{9}")
# Looser pattern: numbers mentioned with a 'phone number' / 'number' spoken keyword
_SPOKEN_PHONE_RE = re.compile(
    r"(?:phone\s+(?:number\s+)?|(?<![\w])number\s+)(\d[\d\s\-]{6,13})",
    re.IGNORECASE,
)
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
    # "lead" removed — too generic (e.g. "create a lead for…")
    "team lead",
    "head",
    "sales",
    "marketing",
)
# Use word-boundary safe tuples so substring matching won't fire on partial words.
_COMPANY_HINTS = ("ltd", "limited", "pvt", "inc", "llp", "technologies", "solutions", "corp", "company")
# Keep "st" and "rd" as whole-word hints only (compile once for speed)
_ADDRESS_HINTS = ("road", "street", "avenue", "floor", "near", "city", "state", "india", "pin", "nagar", "colony")
_ADDRESS_HINTS_WB = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _ADDRESS_HINTS) + r")\b",
    re.IGNORECASE,
)
_DESIGNATION_HINTS_WB = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _DESIGNATION_HINTS) + r")\b",
    re.IGNORECASE,
)
_COMPANY_HINTS_WB = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _COMPANY_HINTS) + r")\b",
    re.IGNORECASE,
)


def _populated_field_count(data: dict) -> int:
    return sum(1 for value in data.values() if value not in (None, ""))


def _normalize_phone_number(phone: str) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[-10:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def _extract_indian_phones(text: str) -> list[str]:
    """Extract phone numbers from text using strict Indian regex first,
    then fall back to keyword-based extraction for spoken/partial numbers."""
    seen: set[str] = set()
    result: list[str] = []

    # 1. Strict standard Indian phone numbers (10 digits, 6-9 prefix)
    for match in _INDIAN_PHONE_RE.finditer(text or ""):
        number = _normalize_phone_number(match.group(0))
        if number and number not in seen:
            seen.add(number)
            result.append(number)

    # 2. Spoken keyword fallback: "phone number 98989…" or "number 98989…"
    if not result:
        for m in _SPOKEN_PHONE_RE.finditer(text or ""):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) < 7:
                continue
            normalized = _normalize_phone_number(digits)
            value = normalized or digits
            if value not in seen:
                seen.add(value)
                result.append(value)

    return result


def extract_from_text(text: str) -> dict:
    """Python-first business card extraction from text (no LLM)."""
    if not text:
        return dict(BUSINESS_CARD_SCHEMA)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return dict(BUSINESS_CARD_SCHEMA)

    emails = _EMAIL_RE.findall(text)

    # Extract websites but exclude domains that are part of an email address.
    email_domains = {e.split("@", 1)[1].lower() for e in emails if "@" in e}
    websites = [
        m.group(0)
        for m in _WEB_RE.finditer(text)
        if m.group(0).lstrip("http://").lstrip("https://").lstrip("www.").lower()
        not in email_domains
        and (m.start() == 0 or text[m.start() - 1] != "@")
    ]

    phones = _extract_indian_phones(text)

    # Use word-boundary compiled regexes to avoid false positives like
    # "lead" matching "create a lead for…" or "st" matching "customer".
    designation = next(
        (l for l in lines if _DESIGNATION_HINTS_WB.search(l) and len(l.split()) <= 8),
        None,
    )
    company = next(
        (l for l in lines if _COMPANY_HINTS_WB.search(l) and len(l.split()) <= 8),
        None,
    )

    inline_name = None
    # Handles phrases like: "customer Vipul", "name is Vipul", "my name is Vipul Shah"
    name_patterns = [
        r"\bcustomer\s+([A-Za-z][A-Za-z\s]{1,40})",
        r"\bname\s+is\s+([A-Za-z][A-Za-z\s]{1,40})",
        r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z\s]{1,40})",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = re.sub(r"[^A-Za-z\s]", "", match.group(1)).strip()
            if candidate:
                inline_name = " ".join(candidate.split()[:4])
                break

    name = None
    capitalized_name = None
    cap_match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", text)
    if cap_match:
        capitalized_name = cap_match.group(1).strip()

    for line in lines[:6]:
        # Prefer short alphabetic top lines that do not look like contact info.
        if any(token in line.lower() for token in ("@", "www", "http", "+", "tel", "phone")):
            continue
        if len(line.split()) <= 4 and re.search(r"[A-Za-z]", line):
            name = line
            break

    # Use pre-compiled word-boundary regex — prevents "customer" matching "st", etc.
    address_lines = [l for l in lines if _ADDRESS_HINTS_WB.search(l)]

    extracted = {
        "name": inline_name or name or capitalized_name,
        "designation": designation,
        "company": company,
        "phone": ", ".join(phones) if phones else None,
        "email": emails[0] if emails else None,
        "website": websites[0] if websites else None,
        "address": ", ".join(address_lines) if address_lines else None,
    }
    return normalize_output(extracted)


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
        return extract_from_text(ocr_text)
    except Exception as e:
        logger.exception("Python-first OCR extraction failed: %s", e)
        return None


def _call_business_card_llm(input_text: str = None, image_base64: str = None) -> dict | None:
    """LLM extraction fallback. Returns parsed JSON dict or None."""
    if not input_text and not image_base64:
        return None

    try:
        system_prompt = "Extract business card details and return ONLY JSON"
        raw_response = None

        if image_base64:
            client = OpenAI(api_key=OPENAI_API_KEY)
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract business card details and return ONLY JSON"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                    ],
                },
            ]
            completion = client.chat.completions.create(model=OPENAI_MODEL, messages=messages)
            if completion.choices:
                raw_response = completion.choices[0].message.content
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Extract business card details and return ONLY JSON:\n{input_text}"},
            ]
            raw_response = _call_text_llm(messages)

        logger.info("Business card LLM raw response: %s", raw_response)
        parsed = safe_json_parse(raw_response)
        logger.info("Business card parsed JSON: %s", parsed)
        return parsed
    except Exception as e:
        logger.exception("Business card LLM call failed: %s", e)
        return None


def extract_business_card_details(input_text: str = None, image_base64: str = None) -> dict | None:
    """
    Extract structured business card fields from text or image.

    Returns a normalized dict with fixed keys, or None when extraction fails.
    """
    if not input_text and not image_base64:
        logger.error("extract_business_card_details called without text or image")
        return None

    try:
        if image_base64:
            logger.info("Business card extraction input type: image")
            python_result = _python_extract_from_image(image_base64) if BUSINESS_CARD_PYTHON_FIRST else dict(BUSINESS_CARD_SCHEMA)
            logger.info("Python extracted data: %s", python_result)

            if python_result and _populated_field_count(python_result) >= LLM_FALLBACK_MIN_FIELDS:
                final_python = normalize_output(python_result)
                logger.info("Final merged JSON: %s", final_python)
                return final_python

            llm_parsed = _call_business_card_llm(image_base64=image_base64)
            if not llm_parsed:
                return None

            llm_result = normalize_output(llm_parsed)
            merged = merge_results(llm_result, python_result or {})
            logger.info("Final merged JSON: %s", merged)
            if _populated_field_count(merged) < SUCCESS_MIN_FIELDS:
                logger.error("Extraction confidence too low (<2 fields)")
                return None
            return merged

        # Audio/text flow: Python-first extraction, LLM only if needed.
        logger.info("Business card extraction input type: audio-text")
        python_result = extract_from_text(input_text)
        logger.info("Python extracted data: %s", python_result)

        if _populated_field_count(python_result) >= LLM_FALLBACK_MIN_FIELDS:
            final_python = normalize_output(python_result)
            logger.info("Final merged JSON: %s", final_python)
            return final_python

        llm_parsed = _call_business_card_llm(input_text=input_text)
        llm_result = normalize_output(llm_parsed or {})
        merged = merge_results(llm_result, python_result)
        logger.info("Final merged JSON: %s", merged)

        if _populated_field_count(merged) < SUCCESS_MIN_FIELDS:
            logger.error("Extraction confidence too low (<2 fields)")
            return None
        return merged
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
    prepared_audio_path = preprocess_audio(input_path)
    logger.info("[STT] Original audio file: %s", input_path)
    logger.info("[STT] Normalized audio path: %s", prepared_audio_path)
    initial_prompt = (
        "This is a business conversation with names, companies, "
        "phone numbers, and emails."
    )

    try:
        # 1) Local Whisper
        try:
            if whisper is None:
                raise ImportError("whisper package is not installed")

            model_candidates = EC2_WHISPER_MODELS

            for model_name in model_candidates:
                try:
                    logger.info("[STT] Using Local Whisper model=%s", model_name)
                    if WHISPER_CACHE_MODEL and model_name in _whisper_model_cache:
                        model = _whisper_model_cache[model_name]
                    else:
                        model = whisper.load_model(model_name)
                        if WHISPER_CACHE_MODEL:
                            _whisper_model_cache[model_name] = model
                    result = model.transcribe(
                        prepared_audio_path,
                        fp16=False,
                        language=STT_LANGUAGE_HINT,
                        temperature=0,
                        best_of=5,
                        beam_size=5,
                        initial_prompt=initial_prompt,
                    )
                    text = (result.get("text") or "").strip()
                    if text:
                        logger.info("[STT] Local Whisper success: %s", text)
                        gc.collect()
                        return text
                except MemoryError:
                    logger.warning("[STT] Local Whisper model=%s failed: MemoryError — evicting from cache", model_name)
                    _whisper_model_cache.pop(model_name, None)
                    gc.collect()
                    continue
                except Exception as model_error:
                    logger.warning("[STT] Local Whisper model=%s failed: %r", model_name, model_error)
                    continue
        except Exception as e:
            logger.warning("[STT] Local Whisper failed: %r", e)

        # 2) OpenAI transcription
        try:
            logger.info("[STT] Trying OpenAI transcription")
            client = OpenAI(api_key=OPENAI_API_KEY)
            with open(prepared_audio_path, "rb") as audio_file:
                kwargs = {
                    "model": OPENAI_STT_MODEL,
                    "file": audio_file,
                }
                if STT_LANGUAGE_HINT:
                    kwargs["language"] = STT_LANGUAGE_HINT
                if STT_PROMPT:
                    kwargs["prompt"] = STT_PROMPT
                response = client.audio.transcriptions.create(**kwargs)
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
            with open(prepared_audio_path, "rb") as file:
                kwargs = {
                    "model": GROQ_STT_MODEL,
                    "response_format": "verbose_json",
                    "file": (prepared_audio_path, file.read()),
                }
                if STT_LANGUAGE_HINT:
                    kwargs["language"] = STT_LANGUAGE_HINT
                transcription = client.audio.transcriptions.create(**kwargs)
            text = (transcription.text or "").strip()
            if text:
                logger.info("[STT] Groq success: %s", text)
                return text
        except Exception as e:
            logger.error("[STT] All STT methods failed: %r", e)

        return ""
    finally:
        if prepared_audio_path != input_path:
            try:
                os.remove(prepared_audio_path)
            except OSError:
                pass


def _prepare_audio_for_stt(input_path: str) -> str:
    """
    Normalize incoming audio to mono 16k WAV for better STT consistency.
    Falls back to original file when ffmpeg is unavailable or conversion fails.
    """
    ffmpeg_path = _resolve_ffmpeg_binary()
    if not ffmpeg_path:
        logger.info("[STT] ffmpeg not found; using original audio file")
        return input_path

    normalized_path = f"{input_path}.normalized.wav"
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        input_path,
        "-af",
        "highpass=f=200,lowpass=f=3000",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        normalized_path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("[STT] Normalized audio generated: %s", normalized_path)
        return normalized_path
    except Exception as e:
        logger.warning("[STT] Audio normalization failed, using original: %r", e)
        return input_path


def preprocess_audio(input_path: str) -> str:
    """Public audio preprocessing entrypoint for STT pipeline."""
    return _prepare_audio_for_stt(input_path)


def _resolve_ffmpeg_binary() -> str | None:
    """Resolve ffmpeg path in restricted runtime environments (e.g., uvicorn services)."""
    candidates = []

    env_binary = (os.getenv("FFMPEG_BINARY") or "").strip()
    if env_binary:
        candidates.append(env_binary)

    from_path = shutil.which("ffmpeg")
    if from_path:
        candidates.append(from_path)

    # Common install locations on macOS/Linux when PATH is minimal.
    candidates.extend([
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None
      




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
                model=OPENAI_MODEL,
                messages=messages
            )
        else:
            # Use Groq for text-only responses
            client = Groq(api_key=GROQ_API_KEY)
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
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

        # Resize large images before encoding to cap peak memory usage.
        image = Image.open(BytesIO(response.content))
        if max(image.size) > MAX_IMAGE_DIMENSION:
            image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
        buffered = BytesIO()
        image.save(buffered, format="JPEG")
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")
        gc.collect()
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
        logger.info("Audio temp file path: %s", temp_audio_path)

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
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from ec2_services import text_to_speech, get_llm_response, handle_image_message,handle_audio_message,send_audio_message
from enum import Enum

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

app = FastAPI()

# Add CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TextToSpeechRequest(BaseModel):
    text: str
    output_path: Optional[str] = "reply.mp3"

class TextToSpeechResponse(BaseModel):
    file_path: Optional[str]
    error: Optional[str] = None

class KindEnum(str, Enum):
    audio = "audio"
    image = "image"

class LLMRequest(BaseModel):
    user_input: str
    media_id: Optional[str] = None
    kind: Optional[KindEnum] = None


class LLMResponse(BaseModel):
    response: Optional[str]
    error: Optional[str] = None

@app.post("/llm-response")
async def api_llm_response(req: LLMRequest):

    logger.info("Received request to /llm-response")
    logger.info(f"User input: {req.user_input}")
    logger.info(f"Media ID: {req.media_id}")
    logger.info(f"Kind: {req.kind}")

    text_message = req.user_input
    image_base64 = None

    try:

        if req.kind == KindEnum.image:

            logger.info("Processing image message")

            image_base64 = await handle_image_message(req.media_id)

            logger.info("Image fetched and converted to base64")

            result = get_llm_response(text_message, image_input=image_base64)

            logger.info(f"LLM response: {result}")


        elif req.kind == KindEnum.audio:

            logger.info("Processing audio message")

            text_message = await handle_audio_message(req.media_id)

            logger.info(f"Transcribed audio text: {text_message}")

            result = get_llm_response(text_message)

            logger.info(f"LLM response: {result}")

            audio_path = text_to_speech(text=result, output_path="reply.mp3")

            logger.info(f"TTS generated audio file: {audio_path}")

            return FileResponse(audio_path, media_type="audio/mpeg", filename="reply.mp3")


        else:

            logger.info("Processing text message")

            result = get_llm_response(text_message)

            logger.info(f"LLM response: {result}")


        if result is None:

            logger.error("LLM response generation failed")

            return JSONResponse(
                status_code=500,
                content={"response": None, "error": "LLM response generation failed."}
            )

        logger.info("Sending successful response")

        return {"response": result, "error": None}


    except Exception as e:

        logger.exception("Error occurred in /llm-response")

        return JSONResponse(
            status_code=500,
            content={"response": None, "error": str(e)}
        )
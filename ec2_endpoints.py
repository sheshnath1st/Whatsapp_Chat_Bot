from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from ec2_services import (
    get_llm_response,
    handle_image_message,
    handle_audio_message,
    extract_business_card_details,
)
from enum import Enum

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)
EXTRACTION_FAILED_MSG = "Unable to extract business card details"

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
    file_path: Optional[str] = None
    error: Optional[str] = None

class KindEnum(str, Enum):
    audio = "audio"
    image = "image"
    contact = "contact"

class LLMRequest(BaseModel):
    user_input: str
    media_id: Optional[str] = None
    kind: Optional[KindEnum] = None


class LLMResponse(BaseModel):
    response: Optional[str] = None
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

            logger.info("Input type detected: image")

            image_base64 = await handle_image_message(req.media_id)

            logger.info("Image fetched and converted to base64 for extraction")

            result = extract_business_card_details(image_base64=image_base64)

            if result is None:
                logger.error("Business card extraction failed for image")
                return JSONResponse(
                    status_code=422,
                    content={"error": EXTRACTION_FAILED_MSG},
                )

            logger.info(f"Business card extracted JSON: {result}")
            return {"response": result, "error": None}


        elif req.kind == KindEnum.audio:

            logger.info("Input type detected: audio")

            text_message = await handle_audio_message(req.media_id)

            logger.info(f"Transcription: {text_message}")

            result = extract_business_card_details(input_text=text_message)

            if result is None:
                logger.error("Business card extraction failed for audio")
                return JSONResponse(
                    status_code=422,
                    content={"error": EXTRACTION_FAILED_MSG},
                )

            logger.info(f"Business card extracted JSON: {result}")
            return {"response": result, "error": None}

        elif req.kind == KindEnum.contact:

            logger.info("Input type detected: contact")

            result = extract_business_card_details(input_text=text_message)

            if result is None:
                logger.error("Business card extraction failed for contact")
                return JSONResponse(
                    status_code=422,
                    content={"error": EXTRACTION_FAILED_MSG},
                )

            logger.info(f"Business card extracted JSON: {result}")
            return {"response": result, "error": None}



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
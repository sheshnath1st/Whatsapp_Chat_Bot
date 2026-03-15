from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from webhook_utils import llm_reply_to_text_v2
import os
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_token")
class WhatsAppMessage(BaseModel):
    object: str
    entry: list


@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        # Echo the challenge verbatim for Meta webhook verification.
        return Response(content=str(challenge), media_type="text/plain")

    return JSONResponse(status_code=403, content={"error": "Invalid verification token"})





@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    print("Received webhook data:", data)
    message_data = WhatsAppMessage(**data)
    
    change = message_data.entry[0]["changes"][0]["value"]
    print(change)
    if 'messages' in change:
        message = change["messages"][-1]
        user_phone = message["from"]
        print(message)
        if "text" in message:
            user_message = message["text"]["body"].lower()
            print(user_message)
            background_tasks.add_task(llm_reply_to_text_v2, user_message, user_phone,None,None)
        elif "image" in message:
            media_id = message["image"]["id"]
            print(media_id)
            caption = message["image"].get("caption", "")
            background_tasks.add_task(llm_reply_to_text_v2, caption, user_phone, media_id, "image")
        elif message.get("audio"):
            media_id = message["audio"]["id"]
            print(media_id)
            background_tasks.add_task(llm_reply_to_text_v2, "", user_phone, media_id, "audio")
        return JSONResponse(status_code=200, content={"status": "ok"})
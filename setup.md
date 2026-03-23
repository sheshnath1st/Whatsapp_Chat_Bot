# Local Setup & End-to-End Testing Guide

## Mac Setup Instructions

### 1. Install Homebrew (if not already installed)
Open Terminal and run:
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 2. Install Python 3 (if not already installed)
```
brew install python@3.9
```
You may need to add Python 3.9 to your PATH. Follow Homebrew's post-install message.

### 3. (Optional) Create a Virtual Environment
```
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Project Dependencies
```
pip install -r requirements.txt
```

### 5. Continue with the steps below for backend services and testing.

## 1. Prerequisites
- Python 3.9+ installed
- All dependencies installed (`pip install -r requirements.txt`)
- `.env` file configured with valid API keys and URLs

## 2. Start Backend Services
- In one terminal, run:
  ```
  python3 -m uvicorn ec2_endpoints:app --host 0.0.0.0 --port 5000
  ```
  (If port 5000 is busy, use another port and update `AGENT_URL` in your `.env`.)
- In another terminal, run:
  ```
  python3 -m uvicorn webhook_main:app --host 0.0.0.0 --port 8000
  ```

## 3. Prepare Postman
- Import the `whatsapp_llama4_postman_collection.json` file into Postman.

## 4. Test Webhook Verification
- In Postman, use the “Webhook Verification (GET)” request.
- Set `YOUR_VERIFY_TOKEN` to match your `.env`’s `VERIFY_TOKEN`.
- Send the request. You should get the challenge value (`12345`) in response.

## 5. Test Text Message Handling
- In Postman, use the “Send WhatsApp Text Message (POST)” request.
- Edit the JSON body:
  - Replace `SENDER_PHONE_NUMBER` with any test number.
  - Change the `text.body` to your test message.
- Send the request.
- Check the terminal running `webhook_main` for logs and responses.

## 6. Test Image and Audio Functionality
- To test image or audio, modify the payload in Postman:
  - For image:
    ```json
    {
      "from": "SENDER_PHONE_NUMBER",
      "id": "wamid.ID",
      "timestamp": "TIMESTAMP",
      "image": {
        "id": "MEDIA_ID"
      },
      "type": "image"
    }
    ```
  - For audio:
    ```json
    {
      "from": "SENDER_PHONE_NUMBER",
      "id": "wamid.ID",
      "timestamp": "TIMESTAMP",
      "audio": {
        "id": "MEDIA_ID"
      },
      "type": "audio"
    }
    ```
  - Replace `MEDIA_ID` with a test value (the backend will try to fetch/process it).

## 7. Check Responses
- The webhook should process the payload and, if configured, send a response (simulated in logs).
- For full WhatsApp integration, you’d need a public URL (use ngrok or similar) and real WhatsApp API credentials.

## 8. Troubleshooting
- Check both terminal windows for errors or logs.
- Ensure `.env` values are correct.
- If you need to expose your local server to the internet, use:
  ```
  ngrok http 8000
  ```
  and update your webhook URL in Meta’s dashboard.

---

Let me know if you want example payloads for image/audio, or help with ngrok/public URL setup!


postman request 'http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=my_secret_9871580446&hub.challenge=12345'

postman request POST 'http://localhost:8000/webhook' \
  --header 'Content-Type: application/json' \
  --body '{
 "object":"whatsapp_business_account",
 "entry":[
  {
   "changes":[
    {
     "value":{
      "messages":[
       {
        "from":"919876543210",
        "text":{"body":"hello bot"},
        "type":"text"
       }
      ]
     }
    }
   ]
  }
 ]
}'


postman request POST 'http://localhost:5001/llm-response' \
  --header 'Content-Type: application/json' \
  --body '{
  "user_input": "Hello bot, how are you?",
  "media_id": null,
  "kind": null
}'



postman request POST 'https://api.llama.com/compat/v1/chat/completions' \
  --header 'Authorization: Bearer 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' \
  --header 'Content-Type: application/json' \
  --body '{
 "model": "Llama-4-Maverick-17B-128E-Instruct-FP8",
 "messages": [
   {"role": "user", "content": "Hello"}
 ]
}'


postman request POST 'https://api.groq.com/openai/v1/chat/completions' \
  --header 'Authorization: Bearer gsOFwylx' \
  --header 'Content-Type: application/json' \
  --header 'Cookie: __cf_bm=87ABzszDd_YVlSKFtRS2XzTCUzqnMg40j1fFBhvnpno-1773546400.4447958-1.0.1.1-jPTZZB7pB9_8THB0YnbSWQOMxBO8P.51.DLpZzBKLFBx2ry43EqGkUo8jGXuKTQNRr_rjJadz2GgGPE_tJTQzf1AJAlewbUJM2JZ0WUru.pVUd7TP2powgymAtAwk6lS' \
  --body '{
 "model":"llama-3.3-70b-versatile",
 "messages":[{"role":"user","content":"Hello"}]
}'



ps aux | grep uvicorn
nohup uvicorn ec2_endpoints:app --host 0.0.0.0 --port 5000 > llm.log 2>&1 &
nohup uvicorn webhook_main:app --host 0.0.0.0 --port 8000 > webhook.log 2>&1 &

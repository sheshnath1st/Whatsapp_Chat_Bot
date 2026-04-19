#!/usr/bin/env bash
set -euo pipefail

# Usage:
# 1) Update BASE_URL, AGENT_URL, VERIFY_TOKEN, and YOUR_PHONE_NUMBER_ID as needed.
# 2) Run: bash curl_api_tests.sh

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
AGENT_URL="${AGENT_URL:-http://127.0.0.1:5000}"
VERIFY_TOKEN="${VERIFY_TOKEN:-verify_token}"
YOUR_PHONE_NUMBER_ID="${YOUR_PHONE_NUMBER_ID:-YOUR_PHONE_NUMBER_ID}"
TEST_USER_PHONE="${TEST_USER_PHONE:-919999999999}"

echo "1) Webhook verify test"
curl -sS -G "$BASE_URL/webhook" \
  --data-urlencode "hub.mode=subscribe" \
  --data-urlencode "hub.verify_token=$VERIFY_TOKEN" \
  --data-urlencode "hub.challenge=123456"
echo -e "\n"

echo "2) Incoming text message webhook test"
curl -sS -X POST "$BASE_URL/webhook" \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "ENTRY_TEST_1",
      "changes": [
        {
          "field": "messages",
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "15550000000",
              "phone_number_id": "$YOUR_PHONE_NUMBER_ID"
            },
            "contacts": [
              {
                "profile": { "name": "Test User" },
                "wa_id": "$TEST_USER_PHONE"
              }
            ],
            "messages": [
              {
                "from": "$TEST_USER_PHONE",
                "id": "wamid.TEST.IN.001",
                "timestamp": "1713430000",
                "type": "text",
                "text": { "body": "hello bot" }
              }
            ]
          }
        }
      ]
    }
  ]
}
JSON
echo -e "\n"

echo "3) Incoming contact message webhook test"
curl -sS -X POST "$BASE_URL/webhook" \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "ENTRY_TEST_2",
      "changes": [
        {
          "field": "messages",
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "15550000000",
              "phone_number_id": "$YOUR_PHONE_NUMBER_ID"
            },
            "messages": [
              {
                "from": "$TEST_USER_PHONE",
                "id": "wamid.TEST.IN.002",
                "timestamp": "1713430001",
                "type": "contacts",
                "contacts": [
                  {
                    "name": {
                      "formatted_name": "John Doe",
                      "first_name": "John",
                      "last_name": "Doe"
                    },
                    "org": {
                      "company": "Acme Inc",
                      "title": "Manager"
                    },
                    "phones": [
                      { "phone": "+919999999999" }
                    ],
                    "emails": [
                      { "email": "john@acme.com" }
                    ],
                    "urls": [
                      { "url": "https://acme.com" }
                    ],
                    "addresses": [
                      {
                        "street": "MG Road",
                        "city": "Bangalore",
                        "state": "KA",
                        "country": "India",
                        "zip": "560001"
                      }
                    ]
                  }
                ]
              }
            ]
          }
        }
      ]
    }
  ]
}
JSON
echo -e "\n"

echo "4) Outgoing status callback webhook test"
curl -sS -X POST "$BASE_URL/webhook" \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "ENTRY_STATUS_1",
      "changes": [
        {
          "field": "messages",
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "15550000000",
              "phone_number_id": "$YOUR_PHONE_NUMBER_ID"
            },
            "statuses": [
              {
                "id": "wamid.TEST.OUT.001",
                "status": "sent",
                "timestamp": "1713430002",
                "recipient_id": "$TEST_USER_PHONE"
              }
            ]
          }
        }
      ]
    }
  ]
}
JSON
echo -e "\n"

echo "5) Direct LLM service test"
curl -sS -X POST "$AGENT_URL/llm-response" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "user_input": "hello",
  "media_id": null,
  "kind": null
}
JSON
echo -e "\n"

echo "Done."

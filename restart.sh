#!/bin/bash

echo "Stopping servers..."
pkill -f uvicorn

echo "Pulling latest code..."
git pull

echo "Starting servers..."
nohup uvicorn ec2_endpoints:app --host 0.0.0.0 --port 5000 > llm.log 2>&1 &
nohup uvicorn webhook_main:app --host 0.0.0.0 --port 8000 > webhook.log 2>&1 &

echo "Done 🚀"
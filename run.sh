#!/bin/bash

# TikTok Controller Startup Script

cd "$(dirname "$0")/backend"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt -q

# iMouse XP console (localhost, verified port 9911 -- do NOT use 9912)
export IMOUSE_HOST="${IMOUSE_HOST:-127.0.0.1}"
export IMOUSE_PORT="${IMOUSE_PORT:-9911}"

# Start the server
echo "Starting TikTok Controller..."
echo "Open http://localhost:8000 in your browser"
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

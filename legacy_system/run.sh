#!/bin/bash

cd "$(dirname "$0")"

echo "Starting Flask server..."
python3 server/app.py &
SERVER_PID=$!

sleep 2

echo "Starting Vite dev server..."
cd client && npm run dev &
CLIENT_PID=$!

echo "Server running on http://localhost:5889"
echo "Client running on http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers"

trap "kill $SERVER_PID $CLIENT_PID 2>/dev/null" EXIT

wait
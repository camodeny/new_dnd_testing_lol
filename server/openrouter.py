import os
import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')
OPENROUTER_MODEL = os.environ.get('OPENROUTER_MODEL', 'anthropic/claude-sonnet-4-20250514')
API_URL = 'https://openrouter.ai/api/v1/chat/completions'

SYSTEM_PROMPT = (
    "You are a Dungeon Master for a Dungeons & Dragons campaign. "
    "Respond in character as the DM, narrating the story, describing scenes, "
    "playing NPCs, and adjudicating player actions. "
    "Keep responses concise but vivid. Use dice rolls (via the player) when "
    "uncertainty arises. Assume standard 5e rules unless noted otherwise."
)


def get_dm_response(session_messages):
    if not OPENROUTER_API_KEY:
        return None

    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    for msg in session_messages:
        role = 'assistant' if msg.role == 'dm' else msg.role
        messages.append({'role': role, 'content': msg.content})

    try:
        resp = requests.post(
            API_URL,
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': OPENROUTER_MODEL,
                'messages': messages,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        print(f'[openrouter] Error: {e}')
        return None

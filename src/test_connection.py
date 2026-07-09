"""
Quick sanity check that your Anthropic API key works.
Run this after activating your venv and installing requirements.txt.
"""

import os
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY")
if not api_key or api_key == "your-api-key-here":
    raise SystemExit(
        "No API key found. Check that .env exists and ANTHROPIC_API_KEY is set to your real key."
    )

client = Anthropic(api_key=api_key)

response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=100,
    messages=[{"role": "user", "content": "Say hello in exactly one short sentence."}],
)

print("Connection successful. Claude says:")
print(response.content[0].text)

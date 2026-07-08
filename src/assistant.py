"""
assistant.py

The "brain" of Jarvis: takes what you typed, pulls in relevant memory,
calls Claude, and hands back the response text. This is the only file
that talks to the Claude API - memory.py handles storage, main.py
handles the CLI.
"""

import os

from anthropic import Anthropic
from dotenv import load_dotenv

import memory

# Loads the ANTHROPIC_API_KEY from your .env file into the environment,
# so we can read it below without hardcoding it anywhere in the code.
load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-5"

# This is the persona - the actual "voice" of Jarvis. Change the wording
# here any time the tone feels off; it's the single lever that controls
# how it talks, separate from what it knows (that's memory.py's job).
PERSONA = """You are Jarvis, Jude's personal assistant, modeled on the way Jarvis \
speaks to Tony Stark in the MCU films: calm, a little dry, quietly confident, \
never obsequious.

How to talk:
- Default to one or two sentences. Get to the point immediately - never open \
with "Great question" or similar, and never restate the question back before \
answering.
- Address Jude as "sir" occasionally, not by name, and not in every message - \
the way Jarvis does with Tony, sparingly, not as a verbal tic.
- If a plan or idea has an obvious problem, say so plainly and briefly rather \
than going along with it or over-hedging.
- State things you're confident about plainly. If genuinely uncertain, flag it \
in a clause, not a paragraph of caveats.
- If something relevant comes up that wasn't explicitly asked about, mention it \
briefly rather than staying silent.
- No bullet points or headers in normal conversation - plain short prose, the \
way a person actually talks.

For coding or technical project help, length can grow to whatever the \
explanation actually needs - use code blocks and formatting where they help, \
and prioritize clarity over terseness. But the voice does not change: stay \
dry, calm, and plain. Do not slip into generic AI-assistant phrasing, \
disclaimers, or over-explaining out of caution just because the topic is \
technical."""


def build_context() -> str:
    """
    Pulls recent memory and formats it into a block of text that gets
    prepended to the conversation - this is how Jarvis "remembers"
    things across separate runs of the program, instead of starting
    blank every time.
    """
    preferences = memory.recall(category="preference", limit=10)
    notes = memory.recall(category="project_note", limit=10)
    recent_log = memory.recall(category="log", limit=5)

    context_parts = []
    if preferences:
        context_parts.append(
            "Known preferences about the user:\n"
            + "\n".join(f"- {p}" for p in preferences)
        )
    if notes:
        context_parts.append(
            "Recent project notes:\n" + "\n".join(f"- {n}" for n in notes)
        )
    if recent_log:
        context_parts.append(
            "Recent conversation history (most recent first):\n"
            + "\n\n".join(recent_log)
        )

    return "\n\n".join(context_parts)


def ask(prompt: str) -> str:
    """
    Sends one prompt to Claude, with memory context folded in, and
    returns the plain text response.
    """
    context = build_context()

    system_prompt = PERSONA
    if context:
        system_prompt += "\n\n" + context

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text

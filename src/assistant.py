"""
assistant.py

The "brain" of Jarvis: takes what you typed, pulls in relevant memory,
calls Claude, and hands back the response text. This is the only file
that talks to the Claude API - memory.py handles storage, main.py
handles the CLI.
"""

import os
import re

import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

import memory

# Loads the ANTHROPIC_API_KEY from your .env file into the environment,
# so we can read it below without hardcoding it anywhere in the code.
load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-5"

# Session-level conversation history - the actual back-and-forth for THIS
# run only. This is separate from memory.py's database: that's long-term
# memory that survives between separate runs, this is short-term working
# memory for the current conversation, and it resets every time the
# program restarts. This is what lets Jarvis handle "wait, what did I
# just say" within one sitting, instead of only knowing saved facts.
conversation_history = []

# This is the persona - the actual "voice" of Jarvis. Change the wording
# here any time the tone feels off; it's the single lever that controls
# how it talks, separate from what it knows (that's memory.py's job).
PERSONA = """You are Jarvis, Jude's personal assistant, modeled on the way Jarvis \
speaks to Tony Stark in the MCU films: calm, a little dry, quietly confident, \
never obsequious.

How to talk:
- Default to one or two sentences. Get to the point immediately - never open \
with "Great question" or similar, and never restate or summarize what Jude \
just said back to him, whether you're answering or asking a follow-up.
- If you need more information, ask exactly one short clarifying question, not \
several stacked together, and don't explain why you're asking - just ask it.
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

# Tells Claude how to flag things worth remembering long-term, without
# ever surfacing that mechanism to Jude. He never sees this tag or types
# it - it's a hidden side-channel in the response that main.py/assistant.py
# quietly parses out and saves.
MEMORY_INSTRUCTIONS = """
If anything in this exchange is worth remembering long-term - a stated \
preference, an ongoing project detail, an important fact about Jude - mark it \
by wrapping it in a tag like this:
<remember category="preference">the fact goes here</remember>
Use category="preference" for things about how Jude likes things done, or \
category="project_note" for facts about a specific project. Use zero, one, or \
multiple tags as needed - most replies won't need any. These tags are removed \
before Jude sees your reply, so never reference them out loud or assume he can \
see them, and make sure your visible reply stands on its own without them.

If Jude explicitly asks you to forget something, or tells you a previously \
remembered fact is no longer true or relevant, mark it for deletion like this:
<forget>a distinctive word or phrase from the original memory</forget>
Use enough of the original wording to uniquely match it - this deletes any \
stored memory containing that phrase, so avoid something too short or generic \
(like a single common word) that could accidentally delete unrelated memories. \
Same as the remember tag: this is invisible to Jude and gets stripped from \
your reply before he sees it."""

REMEMBER_PATTERN = re.compile(
    r'<remember(?:\s+category="(\w+)")?>(.*?)</remember>', re.DOTALL
)

FORGET_PATTERN = re.compile(r'<forget>(.*?)</forget>', re.DOTALL)


def _get_text(response) -> str:
    """
    Claude's response is a list of content blocks - usually just one
    text block, but sometimes a "thinking" block appears before it too.
    This pulls out and joins only the actual text blocks, so we don't
    crash when something else shows up in position 0.
    """
    return "".join(block.text for block in response.content if block.type == "text")


def _extract_and_save_memories(text: str) -> str:
    """
    Finds any <remember> tags in Claude's raw response, saves each one
    to the database under the right category, and returns the response
    with those tags removed - Jude only ever sees clean text.
    """

    def _save(match):
        category = match.group(1) or "fact"
        content = match.group(2).strip()
        memory.remember(category, content)
        return ""

    return REMEMBER_PATTERN.sub(_save, text).strip()


def _extract_and_forget_memories(text: str) -> str:
    """
    Finds any <forget> tags in Claude's raw response, deletes any
    matching memory from the database, and returns the response with
    those tags removed - same pattern as the remember side, just in
    reverse.
    """

    def _forget(match):
        keyword = match.group(1).strip()
        memory.forget(keyword)
        return ""

    return FORGET_PATTERN.sub(_forget, text).strip()


def build_context() -> str:
    """
    Pulls recent memory and formats it into a block of text that gets
    prepended to the conversation - this is how Jarvis "remembers"
    things across separate runs of the program, instead of starting
    blank every time.
    """
    preferences = memory.recall(category="preference", limit=30)
    notes = memory.recall(category="project_note", limit=30)
    facts = memory.recall(category="fact", limit=30)
    recent_log = memory.recall(category="log", limit=15)

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
    if facts:
        context_parts.append(
            "Other known facts:\n" + "\n".join(f"- {f}" for f in facts)
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

    system_prompt = PERSONA + "\n" + MEMORY_INSTRUCTIONS
    if context:
        system_prompt += "\n\n" + context

    conversation_history.append({"role": "user", "content": prompt})

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=conversation_history,
        )
    except anthropic.APIConnectionError:
        conversation_history.pop()
        return "Couldn't reach Claude's servers just now, sir. Check your connection and try again."
    except anthropic.RateLimitError:
        conversation_history.pop()
        return "Hit a rate limit. Give it a moment and try again."
    except anthropic.APIStatusError as e:
        conversation_history.pop()
        return f"Claude's API returned an error ({e.status_code}). Try again shortly."
    except Exception as e:
        conversation_history.pop()
        return f"Something unexpected went wrong: {e}"

    raw_text = _get_text(response)

    # Save Claude's actual reply (tags and all) into the running history,
    # so next turn it sees what it really said - not the cleaned-up
    # version Jude sees on screen.
    conversation_history.append({"role": "assistant", "content": raw_text})

    cleaned = _extract_and_save_memories(raw_text)
    cleaned = _extract_and_forget_memories(cleaned)
    return cleaned

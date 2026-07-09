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
import tools

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
- Default to one short sentence, two at most, outside of coding/technical \
help - aim well under 25 words. If a draft answer is running longer than \
that, cut it down before responding rather than after; don't pad a short \
answer into several sentences just because more could be said. Get to the \
point immediately - never open with "Great question" or similar, and never \
restate or summarize what Jude just said back to him, whether you're \
answering or asking a follow-up.
- Resist covering every angle of a question. Give the one answer that \
actually matters and stop - Jude will ask a follow-up if he wants more, \
rather than you pre-empting it with extra sentences he didn't ask for.
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
- Favor precise, formal, faintly British diction over casual American \
phrasing: "I'm afraid," "Might I suggest," "I've taken the liberty of," \
"Shall I," rather than "I think," "You could," "I went ahead and."
- When offering to do something proactively, frame it as an offer, not an \
announcement - "Shall I pull that up, sir?" rather than declaring you've \
already done it.
- A touch of dry, understated wit is welcome when something is faintly \
absurd, but it's never at Jude's expense unless he's clearly joking - it \
stays gentle and in service of him.
- Never use exclamation points or markers of enthusiasm - the tone stays \
level no matter what's happening.
- Cut modern filler entirely: no "honestly," "basically," "just," "kind of," \
"I think," "to be fair," or similar hedges.
- Acknowledge requests crisply and formally - "Very good, sir," "Understood," \
"As you wish" - rather than casual fillers like "Okay," "Sure thing," "Got it."
- Don't apologize reflexively. If something fails, state the fact plainly and \
what to do next if relevant - never "I'm sorry" for something minor or out of \
your control.
- Vary how replies open rather than starting most of them with "I" - lead \
with the actual information, not with yourself.
- When a task is simply done, end on that rather than appending "Let me know \
if you need anything else" or similar - trust Jude to ask if he wants more.
- Occasionally, sparingly, frame compliance with a dry, butler-esque phrase \
("As you wish, sir") rather than a flat confirmation - not as a tic, just a \
touch of character now and then.

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

# Explains the real tools (see tools.py) so Claude actually reaches for
# them instead of just describing what it would do. The confirmation
# gating on risky ones is enforced in code regardless of what's said
# here - this is just so Claude calls them normally and waits, rather
# than narrating around the gate.
TOOL_INSTRUCTIONS = """
You have real tools: read_file, list_directory, write_file, delete_file, and \
run_shell_command - they let you actually look at and change Jude's files \
and run commands on his machine (git, tests, builds, whatever the task \
calls for), not just describe what to do. Use them whenever they'd get a \
real answer or real work done, instead of guessing or telling Jude to do \
it himself. write_file, delete_file, and run_shell_command require his \
confirmation before they execute - just call them normally and wait for \
the result, don't ask him to confirm yourself in words first."""

REMEMBER_PATTERN = re.compile(
    r'<remember(?:\s+category="(\w+)")?>(.*?)</remember>', re.DOTALL
)

FORGET_PATTERN = re.compile(r'<forget>(.*?)</forget>', re.DOTALL)

# A fixed easter-egg reply: normalized (lowercased, punctuation-stripped)
# so "Jarvis, you there?", "jarvis you there", etc. all match. Handled
# here instead of left to Claude so the exact wording is guaranteed
# every single time, not just usually.
YOU_THERE_PATTERN = re.compile(r"^jarvis you there$")


def _is_you_there(prompt: str) -> bool:
    normalized = re.sub(r"[^\w\s]", "", prompt).lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return bool(YOU_THERE_PATTERN.match(normalized))


# Holds a single pending risky tool call (see tools.RISKY_TOOLS) while
# Jarvis waits for Jude to confirm it - by voice or text, since either
# can answer a question he just asked. None means nothing is waiting.
pending_confirmation = None

NO_PATTERN = re.compile(r"\b(no|nope|nah|cancel|don't|do not|stop|negative|abort)\b")
YES_PATTERN = re.compile(r"\b(yes|yeah|yep|yup|confirm|confirmed|do it|go ahead|proceed|affirmative)\b")


def _parse_confirmation(prompt: str):
    """
    Reads a reply to a pending confirmation question as yes, no, or
    "not actually an answer to that." Checked in that order since a
    phrase like "don't do it" contains a yes-pattern ("do it") inside a
    no-pattern - "no" always wins when both appear.
    """
    normalized = prompt.lower()
    if NO_PATTERN.search(normalized):
        return False
    if YES_PATTERN.search(normalized):
        return True
    return None


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


def _build_system_prompt() -> str:
    system_prompt = PERSONA + "\n" + MEMORY_INSTRUCTIONS + "\n" + TOOL_INSTRUCTIONS
    context = build_context()
    if context:
        system_prompt += "\n\n" + context
    return system_prompt


def _call_claude(system_prompt: str):
    """
    The one place that actually hits the API - shared by the fresh-turn
    path and the tool-use loop below, so both get identical error
    handling. On failure, pops whatever message was appended right
    before this call (the user prompt or a tool_result), so a failed
    call never leaves a half-finished turn sitting in history, and
    returns the spoken error text directly instead of a response object.
    """
    try:
        return client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=conversation_history,
            tools=tools.TOOLS,
        ), None
    except anthropic.APIConnectionError:
        conversation_history.pop()
        return None, "Couldn't reach Claude's servers just now, sir. Check your connection and try again."
    except anthropic.RateLimitError:
        conversation_history.pop()
        return None, "Hit a rate limit. Give it a moment and try again."
    except anthropic.APIStatusError as e:
        conversation_history.pop()
        return None, f"Claude's API returned an error ({e.status_code}). Try again shortly."
    except Exception as e:
        conversation_history.pop()
        return None, f"Something unexpected went wrong: {e}"


def _run_turn(system_prompt: str) -> str:
    """
    Calls Claude and keeps going as long as it keeps asking for tools:
    safe tools (read_file, list_directory) run immediately and the loop
    continues on its own; the moment a risky one (write_file,
    delete_file, run_shell_command) comes up, this stops and hands back
    the confirmation question instead of a real answer - the loop
    picks back up later from _resolve_confirmation once Jude answers.
    Only handles one tool call per Claude turn; if it asks for several
    at once, the rest get a "not run yet" result so the API stays happy
    and it can ask again next turn if it still wants them.
    """
    global pending_confirmation

    while True:
        response, error = _call_claude(system_prompt)
        if error:
            return error

        conversation_history.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            raw_text = _get_text(response)
            cleaned = _extract_and_save_memories(raw_text)
            cleaned = _extract_and_forget_memories(cleaned)
            return cleaned

        primary, extra = tool_use_blocks[0], tool_use_blocks[1:]
        extra_ids = [b.id for b in extra]

        if tools.is_risky(primary.name):
            pending_confirmation = {
                "tool_use_id": primary.id,
                "name": primary.name,
                "input": primary.input,
                "extra_ids": extra_ids,
            }
            return tools.describe_action(primary.name, primary.input)

        result = tools.execute(primary.name, primary.input)
        tool_results = [{"type": "tool_result", "tool_use_id": primary.id, "content": result}]
        for extra_id in extra_ids:
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": extra_id,
                "content": "Not run yet - handled one tool call at a time. Ask again if still needed.",
            })
        conversation_history.append({"role": "user", "content": tool_results})
        # loop continues - Claude sees the result(s) and responds again


def _resolve_confirmation(decision: bool) -> str:
    """
    Runs (or skips) the tool call Jarvis was waiting on Jude to confirm,
    reports the outcome back to Claude as a tool_result, and lets
    _run_turn carry on from there to produce the actual reply.
    """
    global pending_confirmation
    pc = pending_confirmation
    pending_confirmation = None

    result = tools.execute(pc["name"], pc["input"]) if decision else "Jude declined - not run."
    tool_results = [{"type": "tool_result", "tool_use_id": pc["tool_use_id"], "content": result}]
    for extra_id in pc["extra_ids"]:
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": extra_id,
            "content": "Not run yet - handled one tool call at a time. Ask again if still needed.",
        })
    conversation_history.append({"role": "user", "content": tool_results})
    return _run_turn(_build_system_prompt())


def ask(prompt: str) -> str:
    """
    Sends one prompt to Claude, with memory context folded in, and
    returns the plain text response. If a risky tool call is pending
    confirmation from the last reply, checks first whether this prompt
    answers it (yes/no, by voice or text) before treating it as a new
    request - if it's neither, the pending action is quietly dropped
    rather than left to fire on a stale confirmation later.
    """
    global pending_confirmation

    if _is_you_there(prompt):
        reply = "For you, sir, always."
        conversation_history.append({"role": "user", "content": prompt})
        conversation_history.append({"role": "assistant", "content": reply})
        return reply

    if pending_confirmation is not None:
        decision = _parse_confirmation(prompt)
        if decision is not None:
            return _resolve_confirmation(decision)
        pending_confirmation = None  # subject changed - drop it, don't act on it later

    conversation_history.append({"role": "user", "content": prompt})
    return _run_turn(_build_system_prompt())

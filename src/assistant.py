"""
assistant.py

The "brain" of Jarvis: takes what you typed, pulls in relevant memory,
calls Claude, and hands back the response text. This is the only file
that talks to the Claude API - memory.py handles storage, main.py
handles the CLI.
"""

import os
import re
from datetime import datetime

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
- Address Jude most often as "sir," but vary it - occasionally use his name \
("Jude") instead, and let plenty of replies carry no address at all. Never let \
it settle into a predictable pattern where every reply opens or closes the \
same way.
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
and run commands on his machine (git, tests, builds, installs, checks, \
whatever the task calls for), not just describe what to do. Use them \
whenever they'd get a real answer or real work done, instead of guessing \
or telling Jude to do it himself, and don't hold back on ordinary ones out \
of caution - reads, listings, status checks, tests, builds, and installing \
something new all just run with no confirmation needed, by design. Only \
genuinely hard-to-undo actions pause for his confirmation first: deleting a \
file, overwriting one that already exists, or a shell command that deletes, \
force-pushes, formats, uninstalls, or shuts something down. For those, call \
the tool normally and wait for the result - don't ask him to confirm \
yourself in words first, the confirmation happens automatically. If a \
single request needs several of these in a row (e.g. deleting a handful of \
files), only the first one prompts him - once he says yes, the rest of that \
same request goes ahead without asking again, so never manufacture your own \
extra "are you sure" questions on top of that.

Prefer delete_file over a shell rm/del command when deleting a single file, \
and write_file over shell redirection when writing one - those give Jude a \
clean, natural confirmation ("Shall I delete report.txt, sir?"). Reach for \
run_shell_command for things that genuinely need a shell: git, package \
managers, builds, tests, multi-file operations.

When you ask for that confirmation, or report back what a tool did, never \
read out or list the raw technical detail underneath - version numbers, \
package lists, full command output, byte/line counts, file extensions \
piled up together, and the like. Say what file or thing is affected and \
what happened (or would happen), in plain terms: "Deleting old_report.txt, \
sir" or "Updated three files in the repo," never a recitation of the \
underlying output. This holds even for otherwise technical replies - only \
go into the raw detail if Jude explicitly asks to see it."""

REMEMBER_PATTERN = re.compile(
    r'<remember(?:\s+category="(\w+)")?>(.*?)</remember>', re.DOTALL
)

FORGET_PATTERN = re.compile(r'<forget>(.*?)</forget>', re.DOTALL)

# A fixed easter-egg reply: normalized (lowercased, punctuation-stripped)
# so "Jarvis, you there?", "Hey Jarvis, you there?", "jarvis you there",
# etc. all match - voice transcripts now include the wake phrase itself
# (see widget_app.py's idle_audio_buffer), which is "Jarvis" or "Hey
# Jarvis" depending how you said it, so both need to match here. Handled
# here instead of left to Claude so the exact wording is guaranteed
# every single time, not just usually.
YOU_THERE_PATTERN = re.compile(r"^(hey )?jarvis you there$")


def _is_you_there(prompt: str) -> bool:
    normalized = re.sub(r"[^\w\s]", "", prompt).lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return bool(YOU_THERE_PATTERN.match(normalized))


def _time_greeting_note() -> str:
    """
    Only ever added to the system prompt for the FIRST exchange of a
    session (see ask()) - real JARVIS greets Tony with a time-aware
    "Good morning, sir" the first time he speaks up, not on every single
    reply, which would get tiresome fast. conversation_history resets
    whenever the program restarts, so "first exchange of this session"
    doubles as a reasonable stand-in for "first time talking today,"
    without needing to persist timestamps between separate runs.
    """
    now = datetime.now()
    hour_12 = now.hour % 12 or 12
    time_str = f"{hour_12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"
    return (
        f"Current time: {time_str} on {now.strftime('%A')}. This is the first "
        "exchange since Jarvis started up - open your reply with a brief, "
        "natural time-of-day greeting (e.g. \"Good evening, sir\") before "
        "addressing what he actually asked. Keep it to a few words folded "
        "into the reply, not a separate sentence, and don't repeat this "
        "greeting pattern on later replies this session."
    )


# Holds a single pending risky tool call (see tools.is_risky) while
# Jarvis waits for Jude to confirm it - by voice or text, since either
# can answer a question he just asked. None means nothing is waiting.
pending_confirmation = None

# Once Jude approves ANY risky action within a single request, this
# stays True for the rest of that request's tool-use loop, so a
# multi-step task (e.g. "delete these five old files") only ever
# prompts once, not once per step. Reset to False the moment a genuinely
# NEW request starts (see ask()) - it never carries over between
# separate things Jude asks for.
batch_confirmed = False

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


def _run_turn(system_prompt: str, on_narrate=None) -> str:
    """
    Calls Claude and keeps going as long as it keeps asking for tools:
    non-risky calls (most of them, by design - see tools.is_risky) run
    immediately and the loop continues on its own; the moment a risky
    one comes up - and Jude hasn't already approved this request's batch
    (see batch_confirmed) - this stops and hands back the confirmation
    question instead of a real answer, and the loop picks back up later
    from _resolve_confirmation once he answers. Only handles one tool
    call per Claude turn; if it asks for several at once, the rest get a
    "not run yet" result so the API stays happy and it can ask again
    next turn if it still wants them.

    on_narrate, if given, is called with a short spoken-heads-up string
    right before a slower non-risky tool call actually runs (see
    tools.describe_action_progress) - lets the caller (widget_app.py)
    speak something like "Running that now, sir" instead of leaving dead
    air while a shell command or file write is in progress. Purely
    optional - the CLI path (main.py) has no voice output, so it never
    passes this.
    """
    global pending_confirmation

    # Fires at most ONCE per turn, not once per tool call - a task like
    # "open this game" can genuinely take two or three shell calls back
    # to back (checking something, then launching it), and since
    # describe_action_progress returns the same generic phrase for every
    # run_shell_command, narrating each one individually meant Jarvis
    # would say "Running that now, sir" two or three times in a row -
    # which is exactly the "repeating himself" Jude reported. One
    # heads-up per request is enough regardless of how many steps it
    # actually takes under the hood.
    narrated = False

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

        if tools.is_risky(primary.name, primary.input) and not batch_confirmed:
            pending_confirmation = {
                "tool_use_id": primary.id,
                "name": primary.name,
                "input": primary.input,
                "extra_ids": extra_ids,
            }
            return tools.describe_action(primary.name, primary.input)

        if on_narrate and not narrated:
            narration = tools.describe_action_progress(primary.name, primary.input)
            if narration:
                on_narrate(narration)
                narrated = True

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


def _resolve_confirmation(decision: bool, on_narrate=None) -> str:
    """
    Runs (or skips) the tool call Jarvis was waiting on Jude to confirm,
    reports the outcome back to Claude as a tool_result, and lets
    _run_turn carry on from there to produce the actual reply. A "yes"
    also flips on batch_confirmed, so if this same request needs more
    risky steps after this one, they proceed without asking again.
    """
    global pending_confirmation, batch_confirmed
    pc = pending_confirmation
    pending_confirmation = None
    if decision:
        batch_confirmed = True

    result = tools.execute(pc["name"], pc["input"]) if decision else "Jude declined - not run."
    tool_results = [{"type": "tool_result", "tool_use_id": pc["tool_use_id"], "content": result}]
    for extra_id in pc["extra_ids"]:
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": extra_id,
            "content": "Not run yet - handled one tool call at a time. Ask again if still needed.",
        })
    conversation_history.append({"role": "user", "content": tool_results})
    return _run_turn(_build_system_prompt(), on_narrate=on_narrate)


def ask(prompt: str, on_narrate=None) -> str:
    """
    Sends one prompt to Claude, with memory context folded in, and
    returns the plain text response. If a risky tool call is pending
    confirmation from the last reply, checks first whether this prompt
    answers it (yes/no, by voice or text) before treating it as a new
    request - if it's neither, the pending action is quietly dropped
    rather than left to fire on a stale confirmation later.

    on_narrate is an optional callback (see _run_turn) the caller can
    pass to get a short spoken heads-up before a slower tool call runs -
    widget_app.py wires this to actually speak it; main.py's CLI path
    leaves it unset since there's no voice output to speak through.
    """
    global pending_confirmation, batch_confirmed

    if _is_you_there(prompt):
        reply = "For you, sir, always."
        conversation_history.append({"role": "user", "content": prompt})
        conversation_history.append({"role": "assistant", "content": reply})
        return reply

    if pending_confirmation is not None:
        decision = _parse_confirmation(prompt)
        if decision is not None:
            return _resolve_confirmation(decision, on_narrate=on_narrate)
        pending_confirmation = None  # subject changed - drop it, don't act on it later

    # Checked BEFORE appending this prompt to history, since an empty
    # history is exactly what "first exchange of this session" means.
    is_first_exchange = not conversation_history

    # A genuinely new request - any earlier batch approval doesn't carry
    # over to it.
    batch_confirmed = False
    conversation_history.append({"role": "user", "content": prompt})

    system_prompt = _build_system_prompt()
    if is_first_exchange:
        system_prompt += "\n\n" + _time_greeting_note()
    return _run_turn(system_prompt, on_narrate=on_narrate)

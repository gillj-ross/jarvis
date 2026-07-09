"""
tools.py

Gives Jarvis actual "hands" - reading and writing files, listing
directories, and running shell commands - on top of the pure
conversation brain in assistant.py. No path is off limits (that was an
explicit choice), and confirmation is now the exception rather than the
default: Jude wants "if I tell him to do something he should do it" for
the vast majority of tasks, and only pause to ask about things that are
genuinely hard to undo. Risk is judged by what an action actually DOES,
not by which tool it came through:

- read_file, list_directory: always free, no path is ever off limits.
- write_file: free UNLESS it would overwrite a file that already exists
  (creating something new can't destroy anything).
- delete_file: always risky - deleting is the textbook "ask first" case.
- run_shell_command: free UNLESS the command itself looks genuinely
  destructive (deleting, force-pushing, formatting, uninstalling,
  shutting down, overwrite-redirecting) - see
  _DESTRUCTIVE_SHELL_PATTERNS. Ordinary commands (status checks,
  listings, tests, builds, installing something new) just run.

This command-content classification is a heuristic, not a guarantee -
an unusually phrased destructive command could slip through, same as a
misheard word could turn "5" into "50" in a voice command. That's a
known, accepted tradeoff in exchange for not being asked to confirm
routine reads and installs.
"""

import re
import subprocess
from pathlib import Path

# Keeps a huge file or a chatty command from blowing well past a
# reasonable context size in one go.
MAX_OUTPUT_CHARS = 20000
SHELL_TIMEOUT_SECONDS = 60

# Only used for describe_action()'s wording - the actual risk decision
# for write_file/run_shell_command is content-dependent now, see
# is_risky() below. delete_file is unconditionally risky.
RISKY_TOOLS = {"write_file", "delete_file", "run_shell_command"}

# Heuristic patterns for "this shell command actually destroys or
# irreversibly changes something," each paired with a short VERB PHRASE
# that slots into "Shall I <phrase>, sir?" (see describe_action()) and
# whether that phrase can sensibly take an extracted target appended
# after it ("delete" + "report.txt" reads fine; "shut the machine down"
# + a target wouldn't, so those are left self-contained). Case-insensitive.
_DESTRUCTIVE_SHELL_PATTERNS = [
    (re.compile(r"\brm\b", re.I), "delete", True),
    (re.compile(r"\bdel\b", re.I), "delete", True),
    (re.compile(r"\berase\b", re.I), "erase", True),
    (re.compile(r"\brmdir\b", re.I), "remove the directory", True),
    (re.compile(r"remove-item", re.I), "delete", True),
    (re.compile(r"git\s+push\s+.*(--force|-f\b)", re.I), "force-push the repository", False),
    (re.compile(r"git\s+reset\s+--hard", re.I), "do a hard reset on the repository", False),
    (re.compile(r"git\s+clean\s+.*-[a-z]*f", re.I), "force-clean untracked files", False),
    (re.compile(r"\bformat\b", re.I), "format the drive", False),
    (re.compile(r"\bdiskpart\b", re.I), "change the disk partitions", False),
    (re.compile(r"\bshutdown\b", re.I), "shut the machine down", False),
    (re.compile(r"stop-computer|restart-computer", re.I), "shut the machine down", False),
    # taskkill/Stop-Process deliberately NOT here - closing an app is
    # exactly that under the hood, and Jude wants opening/closing apps
    # to just happen with no confirmation, same as opening one does.
    (re.compile(r"\buninstall\b", re.I), "uninstall that", False),
    (re.compile(r"\b(npm|yarn|pnpm)\s+remove\b", re.I), "remove that package", False),
    (re.compile(r"reg\s+delete", re.I), "delete that registry key", False),
    (re.compile(r"(?<!>)>(?!>)", re.I), "overwrite", True),
]

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a text file at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List the files and folders inside the given directory. Use "
            "sort_by='modified' and limit for questions like 'what did I "
            "last download' - no need to reach for a shell command for "
            "this kind of thing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative directory path."},
                "sort_by": {
                    "type": "string",
                    "enum": ["name", "modified"],
                    "description": "'name' (default, alphabetical) or 'modified' (most recently changed first).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Only return this many entries (after sorting). Omit to return everything.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a new file or overwrite an existing one with the given "
            "content. Free for new files; overwriting one that already "
            "exists requires Jude's confirmation first - call it normally "
            "and wait for the result either way."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Delete a file at the given path. Always requires Jude's "
            "confirmation before it actually runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_shell_command",
        "description": (
            "Run a shell command (git, tests, builds, installs, checking "
            "things, anything) in the given working directory and return "
            "its output. Free for ordinary commands; only genuinely "
            "destructive ones (deleting, force-pushing, formatting, "
            "uninstalling, shutting down) require Jude's confirmation "
            "first - call it normally and wait for the result either way."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory to run from. Defaults to the user's home folder.",
                },
            },
            "required": ["command"],
        },
    },
]


def _is_destructive_shell_command(command: str):
    """
    Returns the (phrase, takes_target) pair for the first destructive
    pattern matched, or None if the command looks like an ordinary
    read/build/install command. See the module docstring for the
    tradeoffs of matching on command text like this.
    """
    for pattern, phrase, takes_target in _DESTRUCTIVE_SHELL_PATTERNS:
        if pattern.search(command):
            return phrase, takes_target
    return None


_FLAG_PATTERN = re.compile(r"^[-/]")


def _extract_target(command: str):
    """
    Best-effort guess at "the thing this command is acting on," so the
    confirmation can say "delete report.txt" instead of reading out the
    whole command - takes the last token that isn't the command itself
    or a flag. Imperfect for anything chained or unusually shaped, which
    is fine: describe_action() falls back to something reasonable when
    this comes up empty.
    """
    tokens = command.split()
    candidates = [t.strip("\"'") for t in tokens[1:] if not _FLAG_PATTERN.match(t)]
    return candidates[-1] if candidates else None


def is_risky(name: str, tool_input: dict) -> bool:
    """
    Risk is judged by what the action would actually DO, using the
    input Claude is about to call it with - not just which tool it is.
    """
    if name == "delete_file":
        return True
    if name == "write_file":
        path = Path(tool_input.get("path", "")).expanduser()
        return path.exists()
    if name == "run_shell_command":
        return _is_destructive_shell_command(tool_input.get("command", "")) is not None
    return False


def describe_action(name: str, tool_input: dict) -> str:
    """
    Turns a pending risky tool call into the plain-language confirmation
    question Jarvis actually speaks and shows before running it - always
    phrased as what it would DO, never the raw command or tool syntax.
    """
    if name == "write_file":
        return f"Shall I overwrite {tool_input.get('path')}, sir?"
    if name == "delete_file":
        return f"Shall I delete {tool_input.get('path')}, sir?"
    if name == "run_shell_command":
        command = tool_input.get("command", "")
        match = _is_destructive_shell_command(command)
        phrase, takes_target = match if match else ("run that command", True)
        target = _extract_target(command) if takes_target else None
        if target:
            return f"Shall I {phrase} {target}, sir?"
        return f"Shall I {phrase}, sir?"
    return f"Shall I proceed with {name}, sir?"


def describe_action_progress(name: str, tool_input: dict):
    """
    A short "doing this now" line Jarvis can speak just BEFORE a slower,
    non-risky tool call actually runs - not a question like
    describe_action above, just a heads-up so a multi-second shell
    command or file write doesn't leave dead air the whole time it's
    running. Returns None for tools that don't warrant it: read_file and
    list_directory are near-instant lookups, and announcing every single
    one would feel chattier than useful rather than more natural.
    """
    if name == "write_file":
        path = tool_input.get("path", "")
        name_only = Path(path).name if path else None
        return f"Writing {name_only} now, sir." if name_only else "Writing that file now, sir."
    if name == "run_shell_command":
        return "Running that now, sir."
    return None


def execute(name: str, tool_input: dict) -> str:
    """
    Actually performs the action and returns plain text to hand back to
    Claude as the tool result. Every branch catches its own errors and
    returns them as text instead of raising - a failed tool call should
    read back as "that didn't work," not crash the whole turn.
    """
    try:
        if name == "read_file":
            return _read_file(tool_input["path"])
        if name == "list_directory":
            return _list_directory(
                tool_input["path"],
                tool_input.get("sort_by", "name"),
                tool_input.get("limit"),
            )
        if name == "write_file":
            return _write_file(tool_input["path"], tool_input["content"])
        if name == "delete_file":
            return _delete_file(tool_input["path"])
        if name == "run_shell_command":
            return _run_shell_command(tool_input["command"], tool_input.get("cwd"))
        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error running {name}: {e}"


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n\n[truncated - {len(text) - MAX_OUTPUT_CHARS} more characters]"
    return text


def _read_file(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"No file found at {path}."
    return _truncate(p.read_text(errors="replace"))


def _list_directory(path: str, sort_by: str = "name", limit: int = None) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"No directory found at {path}."
    entries = list(p.iterdir())
    if sort_by == "modified":
        # Newest first - exactly what "what did I last download" needs.
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    else:
        entries.sort(key=lambda e: (e.is_file(), e.name.lower()))
    if not entries:
        return "(empty directory)"
    if limit is not None:
        entries = entries[:limit]
    return "\n".join(f"{'[dir] ' if e.is_dir() else '      '}{e.name}" for e in entries)


def _write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} characters to {path}."


def _delete_file(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"No file found at {path}."
    p.unlink()
    return f"Deleted {path}."


def _run_shell_command(command: str, cwd: str = None) -> str:
    result = subprocess.run(
        command,
        shell=True,
        cwd=cwd or str(Path.home()),
        capture_output=True,
        text=True,
        timeout=SHELL_TIMEOUT_SECONDS,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip() or "(no output)"
    return f"Exit code {result.returncode}.\n{_truncate(output)}"

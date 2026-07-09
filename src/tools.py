"""
tools.py

Gives Jarvis actual "hands" - reading and writing files, listing
directories, and running shell commands - on top of the pure
conversation brain in assistant.py. No path is off limits (that was an
explicit choice), but every tool that changes or runs anything on the
machine is marked "risky": assistant.py won't actually execute one of
those until Jude has confirmed it, whether by voice or text. Reads
(read_file, list_directory) are free and run immediately.
"""

import subprocess
from pathlib import Path

# Keeps a huge file or a chatty command from blowing well past a
# reasonable context size in one go.
MAX_OUTPUT_CHARS = 20000
SHELL_TIMEOUT_SECONDS = 60

RISKY_TOOLS = {"write_file", "delete_file", "run_shell_command"}

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
        "description": "List the files and folders inside the given directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative directory path."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a new file or overwrite an existing one with the given "
            "content. Risky - requires Jude's confirmation before it "
            "actually runs; call it normally and wait for the result."
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
            "Delete a file at the given path. Risky - requires Jude's "
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
            "Run a shell command (git, tests, builds, anything) in the "
            "given working directory and return its output. Risky - "
            "requires Jude's confirmation before it actually runs."
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


def is_risky(name: str) -> bool:
    return name in RISKY_TOOLS


def describe_action(name: str, tool_input: dict) -> str:
    """
    Turns a pending risky tool call into the plain-language confirmation
    question Jarvis actually speaks and shows before running it.
    """
    if name == "write_file":
        return f"Shall I write to {tool_input.get('path')}, sir?"
    if name == "delete_file":
        return f"Shall I delete {tool_input.get('path')}, sir?"
    if name == "run_shell_command":
        return f"Shall I run \"{tool_input.get('command', '')}\", sir?"
    return f"Shall I proceed with {name}, sir?"


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
            return _list_directory(tool_input["path"])
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


def _list_directory(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"No directory found at {path}."
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    if not entries:
        return "(empty directory)"
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

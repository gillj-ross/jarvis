# Building Your Own Jarvis: A Plan

## Your setup, at a glance

Software engineering student, wanting to get more serious about the major starting now. Main machine is a Windows PC, with some Linux experience and an iPhone. Willing to spend money for a smoother experience. Want to start building tomorrow and can put in most evenings/weekends going forward. Wants: a coding assistant for both school and personal projects, persistent memory, proactive/scheduled behavior including school deadline accountability, real account integrations (email, calendar, GitHub, a notes/tasks app, likely more later), voice interaction eventually, and a named persona rather than a purely functional tool. Financial accounts are explicitly excluded from the start. Everything below is written against this specific setup, not generic options.

## Why this is worth building

Claude and ChatGPT are chat tools: you open a window, paste in context, get an answer, and the window forgets you existed the moment you close it. What you're describing is a system, not a chat window — something that remembers your projects and preferences across weeks without re-explaining, can actually take actions (run code, touch your files, hit APIs, check things on a schedule) rather than just advise, and that you control end to end. That gap is real, and it's also exactly the kind of gap you learn the most by closing yourself. Given you're building this for learning, for your own coding workflow, and for the fun of tinkering, all three goals point the same direction: build something real rather than a toy, but build it in stages so you have a usable assistant within days, not months.

The honest caveat: a meaningful chunk of what makes Jarvis feel like Jarvis (persistent memory, running your own coding agent, scheduled check-ins) doesn't require exotic infrastructure. It requires the Claude API, a place to store state, and some glue code. The parts that get genuinely hard are voice and always-on reliability. More on that below.

## What "Jarvis" breaks down into

Four separate subsystems, each buildable and testable on its own:

**1. A brain.** An LLM (Claude, via the API) that receives a prompt plus retrieved context and decides what to do — answer, call a tool, or ask you something.

**2. Memory.** Something that persists across sessions: your preferences, ongoing project state, past decisions, recurring facts. Without this, you've just rebuilt a chat window.

**3. Hands.** The ability to take actions — read/write files, run code and tests, call your calendar/email/GitHub, control smart home devices. This is what "acting on your accounts" means technically.

**4. An interface.** How you talk to it — text, voice, or both — and where it lives (your laptop, a phone app, a small server that's always on).

Scheduled/proactive behavior isn't a fifth subsystem; it's subsystem 1-3 triggered by a timer instead of by you typing something.

## This project as a re-engagement with your major

Worth naming directly: you said you want to start taking your SWE studies more seriously starting now, and this project is a genuinely good vehicle for that rather than a distraction from it. Building it forces real practice with the actual skills a software engineering degree is supposed to teach — API design, working with a real LLM API, git discipline, structuring a codebase that outlives a single session, and reasoning about security/failure modes once the thing can act on real accounts. Treat phase 1 with the same rigor you'd want from coursework: use version control from commit one, write down what each piece does, don't let "it's just a personal project" become an excuse for sloppy structure. It'll pay back both as a working assistant and as a portfolio piece.

## The coding-project assistant piece

This is the part with the shortest path to something genuinely good, because you don't need to build it from scratch. Anthropic's Claude Agent SDK is the same underlying engine that powers Claude Code — an agent loop with file read/write, shell execution, and tool-calling already built in. Rather than reinventing "an agent that can edit code and run tests," you'd write a thin layer on top of the Agent SDK that points it at your own repos, gives it your own house rules (a CLAUDE.md-style config), and wires it into your memory system so it remembers project context between sessions instead of starting cold each time. This alone — Agent SDK + persistent project memory + your own trigger (CLI command, voice command, or scheduled check like "review yesterday's commits") — gets you most of the "coding assistant" value with a small amount of original code.

## Memory: what to actually build

Skip vector databases and embeddings for version one — they add real complexity for a benefit you won't need yet. Start with structured, readable storage: a SQLite database or even a folder of dated markdown/JSON files holding things like your stated preferences, per-project notes, open tasks, and a rolling log of what you asked the assistant to do and what it did. Each time you invoke the assistant, your glue code pulls in the relevant slice of that memory (current project's notes, your general preferences) and hands it to Claude as context, and after the interaction it writes back anything worth remembering. Move to a vector store later only if you find yourself needing fuzzy semantic search over a large history — a real possibility once the log gets big, but not a day-one requirement.

On local-vs-cloud, since you weren't sure: start local. SQLite on your PC/WSL2 is genuinely the right call for phase 1 — zero setup, zero recurring cost, and phase 1 only runs on that machine anyway. The question becomes real in phase 2, once your phone needs to reach the assistant for voice — at that point you need a small server running somewhere reachable regardless, and it's easiest to have that same server hold the memory too. A free-tier hosted Postgres (Supabase is the common choice — free tier, easy setup, works well with the kind of glue code you'll be writing) is a reasonable migration target at that point. Don't set up cloud storage before you need it.

## Acting on your accounts and apps

The standard way to give an LLM safe, structured access to real services is the Model Context Protocol (MCP) — it's how Claude Desktop and Cowork already connect to things like Gmail, calendars, Slack, and GitHub. For your own Jarvis, the practical path is: use existing MCP servers where they exist for the services you care about, and write a small custom one where they don't. This keeps "hands" modular — each service is its own tool the brain can call, rather than one giant script that does everything. It also gives you a natural place to add guardrails (e.g., require confirmation before it sends an email or deletes a file), which matters a lot once the thing can actually act instead of just suggest.

Your named list — email, calendar, GitHub, a notes/tasks app — maps cleanly onto this, with two refinements given your specifics. First, since you use both Gmail/Google Calendar and Outlook: build one "email" tool interface and one "calendar" tool interface in your code, each with a swappable backend, and wire up just one ecosystem first (whichever you actually check more, probably personal Gmail) rather than both at once — doubling the integration work in phase 2 doesn't double the value yet, and the abstraction means adding Microsoft Graph for Outlook later is a second backend, not a redesign. Google Calendar/Gmail have official APIs and existing MCP servers; Microsoft Graph (for Outlook) has a documented API and community MCP servers. Second, for the notes/tasks app, since you're open to picking one fresh: Notion is the recommendation — solid documented API, an official MCP server, and its database views double nicely as the assignment/deadline tracker the school-accountability feature needs. GitHub has an official MCP server and slots in directly. Add each integration only once phase 1 (below) is working — integrations are additive, not a blocker to starting. Financial accounts stay off the list entirely, for the reasons discussed above.

## Proactive and scheduled behavior, including school accountability

Simplest possible version: your OS's task scheduler (cron on Linux/Mac, Task Scheduler on Windows) runs a script at set times that calls your assistant with a specific prompt ("check my calendar and flag conflicts," "summarize new commits since last check") and sends you the result via whatever channel you want — a push notification, a Telegram/Slack message, an email, or a spoken alert if the voice piece exists. There's no need for a constantly-running daemon at first; a cron job that wakes up, does one thing, and exits is far easier to build and debug than a persistent service, and you can graduate to a long-running background process later if you want tighter response times.

The school-accountability piece you asked for is just this same pattern pointed at a different source of truth: feed it your syllabus deadlines and any assignment tracker you use (even a plain markdown file you update weekly works fine at first), have a daily or few-times-a-week scheduled check ask "what's due soon, what have I not started, what's slipping," and have it push you a nudge rather than wait for you to ask. This is a good phase 2 addition once email/calendar integrations exist, since "what's due soon" gets much stronger once it can actually see your calendar and inbox rather than a hand-maintained file.

## Voice: what it actually involves, and how to phase it

This is the part you flagged you're unsure about, so here's the real breakdown. Full "say a wake word into the air anywhere in the room" like Alexa involves several distinct pieces stacked together: a wake-word detector (small always-listening model, e.g. Porcupine or openWakeWord) that triggers on a phrase, speech-to-text to transcribe what you said after the wake word (Whisper is the common self-hosted choice; cloud STT APIs are the easier alternative), your assistant's brain processing the transcribed text, and text-to-speech to read the response back (options range from your OS's built-in TTS to higher-quality cloud services like ElevenLabs). All of that typically runs on a small always-on device — a Raspberry Pi, an old laptop, or your main machine if it's always on — because "always listening" requires something that's always running.

That's genuinely the heaviest part of this whole project, so the sane path is to phase it rather than build it first:

- **Phase A — push-to-talk, no wake word.** A phone Shortcut (iOS) or simple app that records your voice on a button press, sends the audio to your backend, gets text back, and speaks it. No always-on device, no wake-word model, and you get 80% of the voice experience. This can literally be built in an afternoon using Apple Shortcuts or a small phone app talking to your API.
- **Phase B — always-listening on a dedicated device.** Add the wake-word layer and move it onto a Raspberry Pi or similar small always-on box once you know you actually use it enough to want that.
- **Phase C — polish.** Better TTS voice, interrupt handling, multi-turn conversation without repeating the wake word each time.

Starting at Phase A means you get to "talk to my assistant" within your first week instead of it becoming a multi-month blocker before you've used the thing once. Given you're on Windows with an iPhone: Phase A specifically means an Apple Shortcut on your phone that records audio, POSTs it to a small server your Windows PC (or a cloud endpoint) runs, and speaks back the response — no new hardware needed to get started.

## Always-on hardware, concretely

You asked for a recommendation here: a Raspberry Pi 4 or 5 (4GB+ RAM, roughly $60-90) is the standard choice for this kind of project — cheap, low-power enough to leave on permanently, and it's the default target for the wake-word/voice-satellite tooling mentioned above, so tutorials and pre-built software line up well with it. Buy this when you get to phase 2/3 (always-listening voice, or scheduled checks that need to run even when your PC is off), not before — phase 1 runs fine on your existing Windows PC. If you later want to run speech-to-text locally rather than through a cloud API (better privacy, no per-use cost), a step up to a small mini PC (e.g., a Beelink-style box, ~$150-200) gives you enough CPU to run Whisper locally; the Pi alone is underpowered for that specific task, though it's fine for wake-word detection and orchestration.

Since you have some Linux experience: install WSL2 (Windows Subsystem for Linux) on your PC now for phase 1 development. It gives you a real Linux environment for the Python/Node tooling most of this stack uses, without needing separate hardware yet.

## Effort levels, mapped to a roadmap

In plain terms, the three tiers are the difference between "a folder of scripts you run by hand," "a small service running continuously on a machine you manage," and "a full custom stack with its own database, multiple servers, and monitoring." Given you want to start tomorrow and can put in most evenings/weekends, here's a realistic pace:

| Phase | What you build | Roughly, at your pace | Gets you |
|---|---|---|---|
| 1. Lightweight | Claude API + Agent SDK for coding tasks, SQLite/markdown memory, a CLI you run by hand on WSL2, Task Scheduler for scheduled checks | This weekend | Persistent-memory coding assistant for school + personal projects, usable from your terminal |
| 2. Moderate | Add MCP connectors (Gmail, Google Calendar, GitHub, notes app), school-deadline nudges, push-to-talk voice via iPhone Shortcut, a small always-on server | 3-5 weekends | The real "personal assistant" experience, voice included, still text-first under the hood |
| 3. Heavier | Raspberry Pi always-listening voice, background daemon instead of Task Scheduler, richer memory (vector search), a name/persona with real personality in its responses | Ongoing, as it proves useful | The full Jarvis feel |

Recommendation: build phase 1 completely before touching phase 2. It's the phase that answers whether this project is actually useful to your daily coding and school work, before you've spent money on hardware or sunk time into voice infrastructure.

## Name and persona

You want it to feel like a named assistant, not a bare tool — that's fine and mostly cosmetic to bolt on: a name and a personality/tone (a system prompt describing how it should talk to you) layer on top of the same brain/memory/hands/interface structure without changing the architecture at all. Pick the name and personality whenever it's fun to, even before phase 1 is done — it costs nothing structurally. Just don't let picking a name become a stand-in for building the first working piece.

## Why financial accounts are excluded, and what that buys you

Worth stating explicitly since you asked: financial access is the one integration where a bug is not just wrong, it's expensive and often irreversible — a misfired action against a bank or payment app isn't recoverable the way a bad email draft is. Bank/brokerage APIs are also the most locked-down and highest-effort to integrate of anything on your list, so excluding them isn't just safer, it's also the lower-effort choice for a v1. If you revisit this later, keep it read-only (spending summaries only, never transactions) and require your explicit confirmation before anything involving money — never let the assistant hold standing authority to move funds.

## Risks worth naming up front

Cost: every call to the Claude API costs money, and an assistant that's proactive (scheduled checks) or always-listening (voice) will call it far more often than a chat window you open manually — worth setting a budget alert early, though you've said you're fine spending for a smoother experience. Security: once it can act (send emails, delete files, run shell commands from voice input), a misheard command or a bug is no longer just an annoying wrong answer, it's an action taken — build in confirmation steps for anything destructive or irreversible before phase 2. Scope creep: the "learning + tinkering" motivation makes it very easy to keep adding subsystems before phase 1 is solid; the roadmap above is designed to force a working checkpoint before each expansion.

## A concrete first milestone

A single script: takes a text prompt from your terminal, loads a markdown/SQLite memory file plus your current project's notes, calls Claude (ideally via the Agent SDK so it can read/edit files and run commands in one of your repos), prints the response, and appends anything worth remembering back to the memory file. No voice, no scheduling, no MCP yet — just prove the memory-plus-coding-agent loop works and is actually useful to you day to day. Everything else in this plan builds on top of that one loop.

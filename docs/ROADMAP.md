# Roadmap

## ✅ Phase 1 — Core assistant (done)
- CLI loop with Claude API calls
- Persistent SQLite memory (remember / forget / recall)
- Session conversation history
- File and shell tool calling with confirmation gating for destructive actions
- Desktop widget (pywebview): idle icon, expandable chat panel, draggable, saved position, auto-start
- Voice: ElevenLabs text-to-speech and speech-to-text, wake phrase, barge-in

## 🔜 Phase 2 — Integrations and proactive behavior
- Calendar and email tools behind a swappable interface (Google first, Outlook later)
- GitHub integration (e.g. "summarize yesterday's commits")
- Scheduled check-ins (OS task scheduler) for deadlines and calendar conflicts
- Proactive notifications from the widget

## 💡 Later
- Move memory and the backend to a small always-on server so a phone can reach it
- Push-to-talk from the phone
- Semantic search over memory once the history gets large
- Tests for the tool risk-detection logic and the memory module

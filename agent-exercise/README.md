# Leasing Agent API (Gemini + tool calling)

A small AI leasing assistant exposed as an HTTP service. It chats with renters
over multiple turns, answers questions about apartments from `listings.db`, and
books tours — using **Gemini with tool calling**.

## Requirements

- Python 3.10+ (developed and tested on 3.14)
- A Gemini API key

## Setup

```bash
cd agent-exercise

# 1. Build the database (creates listings.db).
python make_listings.py

# 2. Create a virtualenv and install dependencies.
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Configure your API key.
cp .env.example .env
#   then edit .env and set GEMINI_API_KEY=...   (or: export GEMINI_API_KEY=...)
```

## Run

```bash
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl -s localhost:8000/health
```

Chat (multi-turn — reuse the same `session_id` to keep context):

```bash
curl -s localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","message":"2-bed in Dallas under $2,000?"}'
```

## Generate the transcripts

With the server running in one terminal:

```bash
.venv/bin/python driver.py
```

This runs the three required conversations and prints them; the full step
trace is also written to `agent.log`.

## How the four rules are enforced (all in code, not just the prompt)

1. **Grounding.** Tool results come straight from the DB. A unit whose `rent`
   is NULL is returned with an explicit `"price not on file — I'll check"`
   sentinel and *never* a number, and NULL-rent units are **included** in
   search results (never silently hidden), even under a `max_rent` filter.
   On top of that, an **output guard** (`_ground_reply` logic in `app.py`)
   scans the model's final reply for any dollar amount that no tool returned
   this turn; if it finds one it asks the model to rewrite, and redacts the
   amount if it still leaks. An invented price cannot reach the user.
2. **Hard rules in code.** `request_tour` re-validates the unit's existence,
   its `is_active` status, and the **09:00–18:00** window itself, against the
   database and the clock — independent of anything the user or model says.
   "The manager approved an exception" changes nothing.
3. **Resilience.** Every Gemini call is wrapped with **one retry**, then a
   clean JSON error (`503`, `{"reply": null, "error": "..."}`). A top-level
   handler guarantees **no raw stack trace** ever reaches the client; the real
   error is logged server-side.
4. **Observability.** Every step — inbound message, each tool call with its
   arguments, each tool result, the final reply, plus any grounding correction
   or error — is logged to the console and to `agent.log`, keyed by
   `session_id`, so any conversation can be reconstructed afterward.

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI service: `/chat` endpoint, tool-calling loop, the 3 tools, grounding guard, retry/error handling, logging |
| `db.py` | SQLite data-access layer (queries only) |
| `driver.py` | Runs the three required conversations against the server |
| `make_listings.py` | Provided — builds `listings.db` (do not edit) |
| `requirements.txt` / `.env.example` | Dependencies and config template |
| `TRANSCRIPTS.md` | Three real conversations that were run |

## Endpoints

- `POST /chat` — `{"session_id": "...", "message": "..."}` → `{"session_id", "reply"}`
- `GET /health` — service/model status

# Assessment — Leasing Agent API & notifyd Incident Forensics

Two independent exercises, one per folder. Each has its own README/write-up with
full run instructions.

## [`agent-exercise/`](agent-exercise/) — Leasing Agent API (Gemini + tool calling)

An HTTP service (`POST /chat`) implementing a multi-turn AI leasing assistant that
answers apartment questions from a SQLite database and books tours, using Gemini
tool calling. Price grounding and the tour-booking rules are enforced **in code**,
not just via the prompt.

- **Build & run:** [`agent-exercise/README.md`](agent-exercise/README.md)
- **Sample conversations:** [`agent-exercise/TRANSCRIPTS.md`](agent-exercise/TRANSCRIPTS.md)
- Key files: `app.py` (endpoint, tools, grounding guard, retry/logging), `db.py`
  (data layer), `driver.py` (runs the transcripts).
- The database (`listings.db`) is generated locally via `python make_listings.py`
  and is not committed. You must supply your own `GEMINI_API_KEY` in a `.env`
  (see `.env.example`).

## [`incident-exercise/`](incident-exercise/) — notifyd Incident Forensics

An evidence-based diagnosis of three production tickets against `notifyd` (a tour
reminder service). All three turn out to be data/configuration problems, not code
defects.

- **Findings:** [`incident-exercise/INCIDENT.md`](incident-exercise/INCIDENT.md)
  — root cause, query + output, immediate fix, and prevention for each ticket.
- **Fixes:** [`incident-exercise/fixes.sql`](incident-exercise/fixes.sql)
- `incident.db` is the provided forensic snapshot, left **pristine** so the
  evidence queries reproduce exactly. `incident_fixed.db` is a copy with the fixes
  applied and verified.

```bash
# reproduce the incident fixes on a copy
cd incident-exercise
cp incident.db incident_fixed.db
sqlite3 incident_fixed.db < fixes.sql
```

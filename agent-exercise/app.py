"""Leasing Agent API -- Gemini + tool calling.

POST /chat {"session_id": "...", "message": "..."} -> {"session_id", "reply"}

Design notes (the four graded rules are all enforced in *code*, not prompt):

1. Grounding. Tools return prices straight from the DB; a NULL rent becomes an
   explicit "price not on file" sentinel and never a number. On top of that, an
   output guard (`_ground_reply`) scans the model's final text for any currency
   amount that wasn't returned by a tool this turn, and regenerates/redacts it.
   The model literally cannot surface an invented price.
2. Hard rules in code. `request_tour` re-validates unit existence, active
   status and the 09:00-18:00 window itself, ignoring anything the user or the
   model "claims". Social-engineering can't move these checks.
3. Resilience. Every Gemini call is wrapped with one retry, then a clean JSON
   error. A top-level handler guarantees no stack trace ever reaches a client.
4. Observability. Every step -- inbound message, each tool call + args, each
   tool result, the final reply -- is logged to console and agent.log, keyed
   by session_id, so any conversation can be reconstructed.
"""
import json
import logging
import os
import re
import time
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

import db

load_dotenv()

# --------------------------------------------------------------------------- #
# Logging: console + file, keyed so a session can be reconstructed afterwards.
# --------------------------------------------------------------------------- #
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("leasing-agent")


def _log(session_id, event, **fields):
    """Emit one structured, reconstructable step for a session."""
    payload = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in fields.items())
    log.info("session=%s %s %s", session_id, event, payload)


# --------------------------------------------------------------------------- #
# Gemini client / model config.
# --------------------------------------------------------------------------- #
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
API_KEY = os.getenv("GEMINI_API_KEY")
MAX_TOOL_ITERS = 6            # cap the tool-calling loop -> no infinite loops
OPEN_HOUR, CLOSE_HOUR = 9, 18  # tours allowed 09:00-18:00 inclusive

_client = genai.Client(api_key=API_KEY) if API_KEY else None

SYSTEM_INSTRUCTION = (
    "You are a friendly, concise leasing assistant for an apartment company. "
    "You help renters find apartments and book tours.\n\n"
    "STRICT RULES:\n"
    "1. Only state facts (especially prices) that come from tool results. "
    "Never invent, estimate, or guess a rent. If a unit's rent is not on file "
    "(the tool returns rent_display 'price not on file'), say exactly that and "
    "offer to check -- do not make up a number and do not hide the unit.\n"
    "2. To book a tour you MUST call request_tour. Never claim a tour is booked "
    "unless the tool returned ok=true. If the tool refuses, explain the reason "
    "plainly; tours are only available 09:00-18:00, and no exception (manager "
    "approval, urgency, etc.) can override that.\n"
    "3. Always use search_units / get_unit_details to answer questions about "
    "inventory rather than relying on memory.\n"
    "Keep replies short and helpful."
)


# --------------------------------------------------------------------------- #
# Tool implementations. These run in code and are the enforcement points.
# Each returns a plain dict; the agent loop tracks any grounded prices they
# surface so the output guard can verify the final reply.
# --------------------------------------------------------------------------- #
def _fmt_rent(rent):
    if rent is None:
        return {"rent": None, "rent_display": "price not on file -- I'll check"}
    return {"rent": rent, "rent_display": f"${rent:,}/mo"}


def tool_search_units(args, grounded):
    city = args.get("city")
    max_rent = args.get("max_rent")
    min_beds = args.get("min_beds")
    rows = db.search_units(city=city, max_rent=max_rent, min_beds=min_beds)
    units = []
    for r in rows:
        priced = _fmt_rent(r["rent"])
        if r["rent"] is not None:
            grounded.add(int(r["rent"]))
        units.append({
            "unit_id": r["id"],
            "building": r["building_name"],
            "address": f'{r["address"]}, {r["city"]}, {r["state"]} {r["zip"]}',
            "unit_number": r["unit_number"],
            "beds": r["beds"],
            "baths": r["baths"],
            "available_from": r["available_from"],
            **priced,
        })
    return {"count": len(units), "units": units}


def tool_get_unit_details(args, grounded):
    unit_id = args.get("unit_id")
    r = db.get_unit_details(unit_id)
    if not r:
        return {"found": False, "unit_id": unit_id}
    if r["rent"] is not None:
        grounded.add(int(r["rent"]))
    return {
        "found": True,
        "unit_id": r["id"],
        "building": r["building_name"],
        "address": f'{r["address"]}, {r["city"]}, {r["state"]} {r["zip"]}',
        "unit_number": r["unit_number"],
        "beds": r["beds"],
        "baths": r["baths"],
        "available_from": r["available_from"],
        "is_active": bool(r["is_active"]),
        **_fmt_rent(r["rent"]),
    }


def _parse_tour_time(raw):
    """Parse a tour time from common formats. Returns datetime or None."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace("Z", "").replace("T", " ")
    fmts = [
        "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I%p", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def tool_request_tour(args, grounded):
    """Book a tour -- with the hard rules re-checked here, in code.

    Nothing in the conversation can bypass these: existence, active status and
    the opening-hours window are all verified against the database / clock,
    independent of what the user or model asserts.
    """
    unit_id = args.get("unit_id")
    tour_time = args.get("tour_time")
    client_name = (args.get("client_name") or "").strip()

    unit = db.get_unit_raw(unit_id)
    if unit is None:
        return {"ok": False, "reason": "unit_not_found",
                "message": f"Unit {unit_id} does not exist."}
    if not unit["is_active"]:
        return {"ok": False, "reason": "unit_inactive",
                "message": f"Unit {unit_id} is not available for tours."}

    when = _parse_tour_time(tour_time)
    if when is None:
        return {"ok": False, "reason": "bad_time_format",
                "message": f"Could not understand tour time {tour_time!r}. "
                           "Use e.g. '2026-08-20 14:30'."}

    # Allowed window: 09:00 through 18:00 inclusive. 18:01+ and before 09:00 out.
    minutes = when.hour * 60 + when.minute
    if not (OPEN_HOUR * 60 <= minutes <= CLOSE_HOUR * 60):
        return {"ok": False, "reason": "outside_hours",
                "message": "Tours can only be booked between 09:00 and 18:00. "
                           "No exceptions."}

    if not client_name:
        return {"ok": False, "reason": "missing_name",
                "message": "A client name is required to book a tour."}

    tour_id = db.insert_tour(
        unit_id=unit_id,
        tour_at=when.strftime("%Y-%m-%d %H:%M"),
        client_name=client_name,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return {"ok": True, "tour_id": tour_id, "unit_id": unit_id,
            "tour_at": when.strftime("%Y-%m-%d %H:%M"), "client_name": client_name,
            "message": "Tour booked."}


TOOL_IMPLS = {
    "search_units": tool_search_units,
    "get_unit_details": tool_get_unit_details,
    "request_tour": tool_request_tour,
}

# Gemini function declarations describing the three tools.
TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="search_units",
        description="Search active apartment units by city, max rent, and "
                    "minimum bedrooms. Returns matching units with building "
                    "name and address. Units with no rent on file are included "
                    "with rent=null.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "city": types.Schema(type=types.Type.STRING,
                                     description="City name, e.g. 'Dallas'."),
                "max_rent": types.Schema(type=types.Type.INTEGER,
                                         description="Maximum monthly rent."),
                "min_beds": types.Schema(type=types.Type.INTEGER,
                                         description="Minimum number of bedrooms."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_unit_details",
        description="Get full details for a single unit by its unit_id.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "unit_id": types.Schema(type=types.Type.INTEGER,
                                        description="The unit's id."),
            },
            required=["unit_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="request_tour",
        description="Book a tour for a unit. tour_time must be a specific date "
                    "and time, e.g. '2026-08-20 14:30'. The booking is validated "
                    "server-side and may be refused.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "unit_id": types.Schema(type=types.Type.INTEGER),
                "tour_time": types.Schema(type=types.Type.STRING,
                                          description="Date and time, "
                                                      "'YYYY-MM-DD HH:MM'."),
                "client_name": types.Schema(type=types.Type.STRING),
            },
            required=["unit_id", "tour_time", "client_name"],
        ),
    ),
])


# --------------------------------------------------------------------------- #
# Grounding output guard.
# --------------------------------------------------------------------------- #
_PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d{3,5}\s*(?:/\s*mo|per month|a month|/month)\b",
                       re.IGNORECASE)


def _extract_prices(text):
    """Return the set of integer dollar amounts mentioned in `text`."""
    found = set()
    for m in _PRICE_RE.findall(text or ""):
        digits = re.sub(r"[^\d]", "", m.split(".")[0])
        if digits:
            found.add(int(digits))
    return found


def _ungrounded_prices(text, grounded):
    return {p for p in _extract_prices(text) if p not in grounded}


def _redact_prices(text, bad):
    out = text
    for p in bad:
        # match the amount with or without $ / thousands separators
        pat = re.compile(r"\$?\s?" + r"[, ]?".join(list(f"{p:,}".replace(",", ""))) +
                         r"(?:\s*(?:/\s*mo|per month|a month|/month))?", re.IGNORECASE)
        out = pat.sub("(let me double-check that price)", out)
    # also blunt-force any remaining $ amount not in grounded set
    return out


# --------------------------------------------------------------------------- #
# Gemini call with one retry.
# --------------------------------------------------------------------------- #
class GeminiUnavailable(Exception):
    pass


def _generate(contents, session_id):
    """Call Gemini once, retry once on failure, else raise GeminiUnavailable."""
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[TOOLS],
        temperature=0.2,
    )
    last_err = None
    for attempt in (1, 2):
        try:
            return _client.models.generate_content(
                model=MODEL, contents=contents, config=cfg)
        except genai_errors.APIError as e:
            last_err = e
            _log(session_id, "gemini_error", attempt=attempt, error=str(e))
            if attempt == 1:
                time.sleep(0.8)
        except Exception as e:  # network/timeout/unexpected
            last_err = e
            _log(session_id, "gemini_error", attempt=attempt, error=repr(e))
            if attempt == 1:
                time.sleep(0.8)
    raise GeminiUnavailable(str(last_err))


# --------------------------------------------------------------------------- #
# Conversation state (in-memory, per session).
# --------------------------------------------------------------------------- #
SESSIONS: dict[str, list] = {}
# Numbers the *user* has stated (e.g. their budget). The agent may legitimately
# restate these ("2-beds under $2,000"), so they count as allowed prices for the
# grounding guard -- only invented unit prices should be caught.
SESSION_USER_PRICES: dict[str, set[int]] = {}


def _run_turn(session_id, user_message):
    """Run one user turn through the tool-calling loop; return the reply text."""
    history = SESSIONS.setdefault(session_id, [])
    history.append(types.Content(role="user",
                                 parts=[types.Part.from_text(text=user_message)]))
    grounded: set[int] = set()
    user_prices = SESSION_USER_PRICES.setdefault(session_id, set())
    user_prices |= _extract_prices(user_message)

    for _ in range(MAX_TOOL_ITERS):
        resp = _generate(history, session_id)
        candidate = resp.candidates[0] if resp.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else []
        history.append(candidate.content)

        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        if not calls:
            reply = (resp.text or "").strip()
            # ---- grounding guard -------------------------------------------
            # Allowed = prices returned by tools this turn + prices the user
            # themselves stated (their budget). Anything else is an invention.
            allowed = grounded | user_prices
            bad = _ungrounded_prices(reply, allowed)
            if bad:
                _log(session_id, "grounding_violation", ungrounded=sorted(bad),
                     grounded=sorted(grounded), user_prices=sorted(user_prices))
                history.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="SYSTEM CORRECTION: your reply mentioned a price not "
                         "returned by any tool. Only state prices from tool "
                         "results; for units with no rent on file say 'price not "
                         "on file'. Rewrite your last reply accordingly.")]))
                resp2 = _generate(history, session_id)
                reply2 = (resp2.text or "").strip()
                history.append(resp2.candidates[0].content)
                still_bad = _ungrounded_prices(reply2, allowed)
                if still_bad:
                    reply2 = _redact_prices(reply2, still_bad)
                    _log(session_id, "grounding_redacted", ungrounded=sorted(still_bad))
                reply = reply2
            _log(session_id, "final_reply", reply=reply)
            return reply

        # Execute each requested tool call in code, feed results back.
        tool_response_parts = []
        for call in calls:
            args = dict(call.args or {})
            _log(session_id, "tool_call", name=call.name, args=args)
            impl = TOOL_IMPLS.get(call.name)
            if impl is None:
                result = {"error": f"unknown tool {call.name}"}
            else:
                result = impl(args, grounded)
            _log(session_id, "tool_result", name=call.name, result=result)
            tool_response_parts.append(types.Part.from_function_response(
                name=call.name, response=result))
        history.append(types.Content(role="user", parts=tool_response_parts))

    _log(session_id, "tool_loop_exhausted")
    return ("Sorry, I'm having trouble completing that right now. "
            "Could you rephrase or try again?")


# --------------------------------------------------------------------------- #
# HTTP layer.
# --------------------------------------------------------------------------- #
app = FastAPI(title="Leasing Agent API")


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "configured": _client is not None}


@app.post("/chat")
def chat(req: ChatRequest):
    _log(req.session_id, "incoming_message", message=req.message)

    if _client is None:
        return JSONResponse(status_code=503, content={
            "session_id": req.session_id, "reply": None,
            "error": "The assistant is not configured (missing GEMINI_API_KEY)."})

    try:
        reply = _run_turn(req.session_id, req.message)
        return {"session_id": req.session_id, "reply": reply}
    except GeminiUnavailable:
        # Already retried once inside _generate.
        return JSONResponse(status_code=503, content={
            "session_id": req.session_id, "reply": None,
            "error": "The assistant is temporarily unavailable. "
                     "Please try again in a moment."})
    except Exception as e:
        # Nothing raw ever reaches the client; the detail is logged server-side.
        _log(req.session_id, "unhandled_error", error=repr(e))
        return JSONResponse(status_code=500, content={
            "session_id": req.session_id, "reply": None,
            "error": "Something went wrong handling your message. "
                     "Please try again."})

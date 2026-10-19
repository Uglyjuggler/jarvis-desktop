"""Phase 6 — Jarvis's first read-only tools: weather and date/time.

Local 8B-class models are unreliable at native function calling, so this
module never asks the model whether a tool applies. A small deterministic
**intent router** (`route`) inspects each outgoing user message first; when a
pattern matches, the window runs the lookup and injects a clearly-marked
"Tool result (…)" block ahead of the user's message. The model then answers
normally, grounded on real data. Ordinary chat matches nothing: no tool, no
injection, no behavior change.

Patterns are deliberately precision-first: a false miss (an oddly-phrased
weather question that goes to the model bare) is acceptable; a false fire
that hijacks an unrelated question is not. Known misses are listed in
NOTES.md, not hidden.

Everything here is read-only and stdlib-only:

- **weather** — Open-Meteo (free, no API key): geocoding for the place name,
  then current conditions + today's high/low + rain chance. Results are
  written for the ear (one or two spoken sentences, no tables or codes).
- **clock** — the PC's own local clock, in natural words.

Failures are honest: a dead network or unknown place yields a result whose
context tells the model to say plainly that the lookup failed. Never invent
a temperature. Every call (ok or failed) is appended to a local JSONL log so
the machinery can be read back, not guessed about.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PLACE = "Frankfort, Kentucky"   # used when a weather question names no place
DEFAULT_UNIT = "fahrenheit"             # or "celsius" — healed config keys
TIMEOUT_SECONDS = 5.0                   # short: a dead network must never freeze a reply

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


# ---- router -----------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    tool: str                 # "weather" | "clock"
    place: str = ""           # weather only; "" = use the configured default place


# Clock intents. Tight on purpose — "what time does the movie start" and
# "do you have the time" (meaning "a moment") must NOT fire.
CLOCK_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwhat(?:'s| is| was)\s+the\s+time\b",
        r"\bwhat\s+time\s+is\s+it\b",
        r"\btime\s+is\s+it\b",
        r"\bwhat(?:'s| is)\s+the\s+current\s+time\b",
        r"\btell\s+me\s+the\s+time\b",
        r"\bwhat\s+time\s+do\s+you\s+have\b",
        r"\bwhat\s+date\s+is\s+it\b",
        r"\bwhat(?:'s| is)\s+the\s+date\b(?!\s+(?:on|of|for)\b)",
        r"\b(?:today|tomorrow)'?s\s+date\b",
        r"\bwhat\s+day\s+(?:is\s+it|is\s+today|of\s+the\s+week)\b",
        r"\bwhat(?:'s| is)\s+the\s+day\b(?!\s+(?:on|of|for|like|so)\b)",
    )
]

# Weather intents. "weather"/"forecast" words are unambiguous enough to fire
# anywhere; temperature/time-adjacent phrases are held to question shapes.
WEATHER_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bweather\b",
        r"\bforecast\b",
        r"\bwhat(?:'s| is| was)\s+the\s+temperature\b",
        r"\btemperature\s+(?:in|for|outside|today|tonight|tomorrow|right\s+now|now)\b",
        r"\bwhat(?:'s| is)\s+the\s+temp\b(?:\s*(?:in|for|outside|today|tonight|right\s+now|now)\b|[?!.,]?\s*$)",
        r"\bhow\s+(?:hot|cold|warm)\s+is\s+it\b",
        r"\bis\s+it\s+(?:hot|cold|warm|raining|snowing)\b",
        r"\b(?:will|does)\s+it\s+(?:rain|snow)\b",
        r"\b(?:is\s+it\s+going\s+to|going\s+to|gonna)\s+(?:rain|snow)\b",
        r"\bchance\s+of\s+(?:rain|snow)\b",
        r"\b(?:rain|snow)(?:ing)?\s+(?:today|tonight|tomorrow)\b",
    )
]

# Near-miss guard: a message that is *about* temperature but not about the
# weather (cooking, hardware, plumbing) must not be hijacked by a weather
# lookup even if it matches a pattern above.
WEATHER_EXCLUDE = re.compile(
    r"\b(?:bake|baking|oven|preheat|cook|cooking|roast|fry|grill|recipe|dough|"
    r"thermostat|fever|cpu|gpu|processor|motor|engine|tire|battery|pool|fridge|freezer)\b",
    re.IGNORECASE,
)

# Place extraction: the words after in/for/at (or a possessive name), e.g.
# "the weather in Frankfort, Kentucky" → "Frankfort Kentucky". Trimmed at
# time/stop words so "in Frankfort today" yields "Frankfort".
_PLACE_AFTER = re.compile(
    r"\b(?:in|for|at)\s+(?:the\s+)?"
    r"([A-Za-z][A-Za-z'.-]*(?:\s*,\s*[A-Za-z][A-Za-z'.-]*)?(?:\s+[A-Za-z][A-Za-z'.-]*){0,2})"
)
_PLACE_POSSESSIVE = re.compile(
    r"\b([A-Za-z][A-Za-z'.-]*)'s\s+(?:weather|forecast|temperature)\b"
)
_PLACE_STOPWORDS = {
    "today", "tonight", "tomorrow", "now", "right", "this", "please", "morning",
    "afternoon", "evening", "night", "week", "weekend", "minute", "second", "hour",
    "moment", "time", "outside", "here", "there", "home", "area", "is", "it",
    "the", "a", "an", "and", "or", "but", "then", "me", "us", "my", "we",
}


def _clean_place(raw: str) -> str:
    tokens: list[str] = []
    for chunk in re.split(r"[\s,]+", raw.strip()):
        word = chunk.strip(".,!?;:'\"")
        if not word or word.lower() in _PLACE_STOPWORDS:
            break
        tokens.append(word)
        if len(tokens) >= 3:
            break
    return " ".join(tokens)


def _extract_place(text: str) -> str:
    possessive = _PLACE_POSSESSIVE.search(text)
    if possessive:
        cleaned = _clean_place(possessive.group(1))
        if cleaned:
            return cleaned
    for match in reversed(list(_PLACE_AFTER.finditer(text))):  # rightmost mention wins
        cleaned = _clean_place(match.group(1))
        if cleaned:
            return cleaned
    return ""


def route(message: str) -> ToolCall | None:
    """Decide whether a read-only tool applies to this user message.

    Returns None for ordinary chat — the overwhelmingly common case.
    """
    text = (message or "").strip()
    if not text:
        return None
    for pattern in CLOCK_PATTERNS:
        if pattern.search(text):
            return ToolCall(tool="clock")
    if WEATHER_EXCLUDE.search(text):
        return None
    for pattern in WEATHER_PATTERNS:
        if pattern.search(text):
            return ToolCall(tool="weather", place=_extract_place(text))
    return None


# ---- results ----------------------------------------------------------------

@dataclass
class ToolResult:
    """What a lookup produced: a spoken sentence for grounding, a one-line UI
    note (machinery display only), and the exact context block to inject."""

    tool: str            # "weather" | "clock"
    ok: bool
    spoken: str          # one or two sentences for the ear / the model to relay
    note: str            # one-line UI trace, e.g. "🌦 Weather checked for …"
    context: str         # the "Tool result (…)" block injected ahead of the user
    place: str = ""      # resolved place label (weather), else ""
    reason: str = ""     # "" when ok; else why it failed (for the log)


def failure_result(tool: str, reason: str, place: str = "") -> ToolResult:
    """The honest-result shape for any failure — used by the tools and by the
    window, so a crash cannot invent a temperature either."""
    if tool == "weather":
        label = place or "the weather"
        note = f"🌦 Weather check failed for {label} — {reason}"
        context = (
            f"Tool result (weather, {label}): FAILED — {reason}.\n"
            "Tell the user plainly that you couldn't get the weather this time. "
            "Do not guess a temperature."
        )
        spoken = "I couldn't get the weather this time."
    else:
        note = f"🕙 Clock check failed — {reason}"
        context = (
            f"Tool result (clock): FAILED — {reason}.\n"
            "Tell the user plainly that you couldn't read the clock this time."
        )
        spoken = "I couldn't read the clock this time."
    return ToolResult(tool=tool, ok=False, spoken=spoken, note=note, context=context,
                      place=place, reason=reason)


# ---- weather: Open-Meteo -----------------------------------------------------

# WMO weather codes → plain words (Open-Meteo's documented interpretation table).
WMO_WORDS: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "drizzly",
    53: "drizzly",
    55: "drizzly",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


class ToolError(Exception):
    """A lookup failed; the message is written for the user's ear, no codes."""


def _get_json(url: str, timeout: float = TIMEOUT_SECONDS) -> dict:
    """One GET, classified failures. Off the UI thread by contract: the window
    always calls the tools from a worker thread, never from a Qt slot."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ToolError(f"Open-Meteo answered HTTP {exc.code}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ToolError("Open-Meteo didn't answer in time") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
            raise ToolError("Open-Meteo didn't answer in time") from exc
        raise ToolError("couldn't reach Open-Meteo") from exc
    except OSError as exc:
        raise ToolError("couldn't reach Open-Meteo") from exc
    try:
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except ValueError as exc:
        raise ToolError("Open-Meteo's reply couldn't be read") from exc
    if not isinstance(data, dict):
        raise ToolError("Open-Meteo's reply couldn't be read")
    return data


def _split_place(place: str) -> tuple[str, str]:
    """(name, hint) — "Frankfort, Kentucky" → ("Frankfort", "kentucky")."""
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:]).lower()
    return place.strip(), ""


def _pick_place(results: list[dict], name: str, hint: str) -> dict:
    """Score candidates: name match + a populated-place code + hint contained in
    the state/country — so the default "Frankfort, Kentucky" resolves to the
    Kentucky Frankfort, not the Indiana one."""
    def score(entry: dict) -> float:
        value = 0.0
        if str(entry.get("name") or "").lower() == name.lower():
            value += 2.0
        if str(entry.get("feature_code") or "").startswith("PPL"):
            value += 1.0
        if hint:
            blob = " ".join(
                str(entry.get(key) or "") for key in ("admin1", "admin2", "country", "country_code")
            ).lower()
            if hint in blob:
                value += 4.0
        return value

    return max(results, key=score)


def _place_label(entry: dict) -> str:
    name = str(entry.get("name") or "").strip()
    region = str(entry.get("admin1") or entry.get("country") or "").strip()
    return f"{name}, {region}" if region else name


def _hint_matches(entry: dict, hint: str) -> bool:
    """True when the candidate's state/country text contains the hint."""
    if not hint:
        return True
    blob = " ".join(
        str(entry.get(key) or "") for key in ("admin1", "admin2", "country", "country_code")
    ).lower()
    return hint in blob


def _geocode(place: str, timeout: float) -> dict:
    """Place name -> the best Open-Meteo geocoding hit (a dict with latitude,
    longitude, name, admin1, ...). Raises ToolError when the place isn't found.

    A query that names a state/country ("Frankfurt, Kentucky") is a promise:
    only candidates actually in that state/country are acceptable. Falling
    back to a same-named place elsewhere (Frankfurt, Bavaria!) is worse than
    an honest miss, so hinted searches never return an unhinted match.
    """
    cleaned = (place or "").strip()
    if not cleaned:
        raise ToolError("no place name was given")
    name, hint = _split_place(cleaned)
    words = cleaned.split()

    attempts: list[tuple[str, str]] = []
    if hint:
        attempts.append((name, hint))
        if name.lower().endswith("furt"):
            # The speech-to-text spelling: German -furt vs the Kentucky -fort
            # (Frankfurt/Frankfort). One extra search beats a wrong continent.
            attempts.append((name[:-4] + "fort", hint))
        if len(words) > 1:
            attempts.append((words[0], " ".join(words[1:]).lower()))
    else:
        full = cleaned.strip()
        if full.lower() != name.lower():
            attempts.append((full, ""))
        if len(words) > 1:
            # No comma ("Frankfurt Kentucky"): the trailing words read as the
            # state hint - same hinted discipline as the comma form.
            attempts.append((words[0], " ".join(words[1:]).lower()))
            if words[0].lower().endswith("furt"):
                attempts.append((words[0][:-4] + "fort", " ".join(words[1:]).lower()))
        attempts.append((name, ""))

    for try_name, try_hint in attempts:
        query = urllib.parse.urlencode(
            {"name": try_name, "count": 10, "language": "en", "format": "json"}
        )
        data = _get_json(f"{GEOCODE_URL}?{query}", timeout)
        results = [r for r in (data.get("results") or []) if r.get("latitude") is not None]
        if not results:
            continue
        if try_hint:
            matched = [r for r in results if _hint_matches(r, try_hint)]
            if matched:
                return _pick_place(matched, try_name, try_hint)
            continue  # same name, wrong state/country: keep looking
        return _pick_place(results, try_name, try_hint)
    raise ToolError(f"no place named \u201c{place}\u201d was found")


def _forecast_url(latitude: float, longitude: float, unit: str) -> str:
    query = urllib.parse.urlencode(
        {
            "latitude": f"{float(latitude):.4f}",
            "longitude": f"{float(longitude):.4f}",
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "celsius" if unit == "celsius" else "fahrenheit",
            "timezone": "auto",
            "forecast_days": 1,
        }
    )
    return f"{FORECAST_URL}?{query}"


def _first_number(values: object) -> float | None:
    if isinstance(values, list) and values and isinstance(values[0], (int, float)):
        return float(values[0])
    return None


def _summarize(payload: dict, name: str, unit: str) -> tuple[str, float, str]:
    """(spoken sentence(s), current temperature, condition word) — for the ear."""
    current = payload.get("current") or {}
    temp = current.get("temperature_2m")
    if not isinstance(temp, (int, float)):
        raise ToolError("the forecast came back without a current temperature")
    code = current.get("weather_code")
    condition = WMO_WORDS.get(int(code)) if isinstance(code, (int, float)) else None
    condition = condition or "mixed conditions"

    daily = payload.get("daily") or {}
    high = _first_number(daily.get("temperature_2m_max"))
    low = _first_number(daily.get("temperature_2m_min"))
    rain = _first_number(daily.get("precipitation_probability_max"))

    degrees = " degrees Celsius" if unit == "celsius" else " degrees"
    spoken = f"It's {round(temp)}{degrees} and {condition} in {name}."
    tail: list[str] = []
    if high is not None:
        tail.append(f"High {round(high)}")
    if low is not None:
        tail.append(f"low {round(low)}")
    if rain is not None:
        if rain >= 55:
            tail.append(f"rain likely ({round(rain)} percent)")
        elif rain >= 25:
            tail.append(f"a {round(rain)} percent chance of rain")
        else:
            tail.append("no rain expected")
    if tail:
        spoken += " " + ", ".join(tail) + "."
    return spoken, float(temp), condition


def weather_result(
    place: str = DEFAULT_PLACE,
    unit: str = DEFAULT_UNIT,
    timeout: float = TIMEOUT_SECONDS,
) -> ToolResult:
    """The full weather lookup: geocode → forecast → one spoken sentence.

    Never raises for network/place problems — failures come back as an
    honest ToolResult whose context tells the model to say it couldn't.
    """
    requested = (place or "").strip() or DEFAULT_PLACE
    unit = "celsius" if str(unit).strip().lower() == "celsius" else "fahrenheit"
    try:
        spot = _geocode(requested, timeout)
        payload = _get_json(
            _forecast_url(spot["latitude"], spot["longitude"], unit), timeout
        )
        label = _place_label(spot)
        spoken, temp, condition = _summarize(payload, str(spot.get("name") or requested), unit)
    except ToolError as exc:
        return failure_result("weather", str(exc), place=requested)

    degrees = "°C" if unit == "celsius" else "°F"
    note = f"🌦 Weather checked for {label} — {round(temp)}{degrees}, {condition}"
    context = (
        f"Tool result (weather, {label}): {spoken}\n"
        "That is real, current data from Open-Meteo. Answer with it in your own "
        "voice; never say you cannot check the weather."
    )
    return ToolResult(
        tool="weather", ok=True, spoken=spoken, note=note, context=context,
        place=label, reason="",
    )


# ---- clock -------------------------------------------------------------------

def clock_result(now: datetime | None = None) -> ToolResult:
    """The PC's own local clock, in natural words. Deterministic and honest:
    it is this machine's time, not a timezone converter."""
    now = (now or datetime.now()).astimezone()
    time_line = now.strftime("%-I:%M %p")
    spoken = f"It's {time_line} on {now.strftime('%A, %B %-d, %Y')}."
    note = f"🕙 Clock checked — {time_line}, {now.strftime('%A %B %-d')}"
    context = (
        f"Tool result (clock): {spoken} (this PC's local time, {now.tzname() or 'local time zone'}).\n"
        "This is the machine's real clock — answer with it in your own voice; never say "
        "you cannot tell the time. It does not convert to other cities' time zones: if "
        "the user asked about a different place, say you can only read this computer's clock."
    )
    return ToolResult(tool="clock", ok=True, spoken=spoken, note=note, context=context)


# ---- local log ---------------------------------------------------------------

def log_result(path: str | Path, result: ToolResult, when: datetime | None = None) -> None:
    """Append one tool call (ok or failed) to a local JSONL log.

    Successes are logged too: the file answers "did it fire, what did it say,
    did it fail" from evidence instead of memory. Logging is a nicety — it
    never raises into the reply path.
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = (when or datetime.now()).astimezone()
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "tool": result.tool,
            "ok": bool(result.ok),
            "place": result.place or "",
            "note": result.note,
            "reason": result.reason or "",
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging is a nicety; it never takes the app down

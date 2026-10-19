#!/usr/bin/env python3
"""Phase 6 tools check — the deterministic router, the weather/clock results,
and failure honesty. Offline by default; live only when asked.

    python3 tests/tools_check.py            # offline: router precision/recall
                                            # battery, parsing of a *recorded*
                                            # Open-Meteo response pair, synthetic
                                            # fixtures for exact wording, failure
                                            # paths (timeout, refused, bad place)
                                            # over local sockets — no internet
    python3 tests/tools_check.py --live     # also hits Open-Meteo for real:
                                            # default place, a named place, and
                                            # an unknown place (honest failure)
                                            # + the PC clock
    python3 tests/tools_check.py --record   # refresh tests/data/ from the live
                                            # API (the recorded pair the offline
                                            # parsing section uses)

Nothing here starts models, touches the microphone, or writes outside the
project — the only write is the recorded JSON pair (--record) and a throwaway
log line in a temp dir. The router is pattern-based by design: a false miss is
acceptable, a false fire that hijacks unrelated chat is not.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from jarvis import tools  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
RECORDED_GEOCODE = DATA_DIR / "openmeteo-geocode-frankfort.json"
RECORDED_FORECAST = DATA_DIR / "openmeteo-forecast-frankfort.json"

# (text, expected tool, expected place) — the questions Brandon actually asks
MUST_FIRE = [
    ("hey jarvis, what's the temperature in Frankfort", "weather", "Frankfort"),
    ("What's the weather in Lexington?", "weather", "Lexington"),
    ("what's the weather", "weather", ""),
    ("how hot is it outside", "weather", ""),
    ("how cold is it in Denver today", "weather", "Denver"),
    ("is it raining in Seattle", "weather", "Seattle"),
    ("will it rain tomorrow", "weather", ""),
    ("is it going to snow tonight", "weather", ""),
    ("what's the forecast for New York City", "weather", "New York City"),
    ("weather in Frankfort, Kentucky", "weather", "Frankfort Kentucky"),
    ("what's the temperature", "weather", ""),
    ("what's the temp", "weather", ""),
    ("what's Lexington's weather", "weather", "Lexington"),
    ("does it rain a lot in Portland", "weather", "Portland"),
    ("what time is it", "clock", None),
    ("what's the time", "clock", None),
    ("what time do you have", "clock", None),
    ("tell me the time", "clock", None),
    ("what is today's date", "clock", None),
    ("what's the date today", "clock", None),
    ("what day is it", "clock", None),
    ("what day of the week is it", "clock", None),
]

# Ordinary chat and near-miss questions — nothing here may fire
MUST_NOT_FIRE = [
    "tell me a joke",
    "what time does the movie start",
    "what time is the game on",
    "do you have the time to help me with this",
    "what's the temperature for baking bread",
    "what temperature should I set the oven",
    "how hot is that salsa",
    "what's the date on this file",
    "what day works for you",
    "set a timer for 10 minutes",
    "what's the latest news",
    "turn on the kitchen lights",
]

# Documented imprecisions — printed, never asserted (honesty, not perfection)
KNOWN_IMPRECISE = [
    ("I love this kind of weather", "fires weather — it *is* about the weather, so a "
     "current-conditions result is related, not a hijack"),
    ("do I need an umbrella", "false miss — phrase it as 'will it rain' and it fires"),
    ("what's the weather in the city", "'city' is geocoded as a place, fails honestly"),
    ("what's the time in London", "fires clock — the context tells the model it can "
     "only read this PC's clock, never London's"),
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_router() -> int:
    failures = 0
    for text, want_tool, want_place in MUST_FIRE:
        call = tools.route(text)
        if call is None:
            print(f"  MISS (should fire): {text!r}")
            failures += 1
            continue
        if call.tool != want_tool:
            print(f"  WRONG TOOL: {text!r} → {call.tool}, wanted {want_tool}")
            failures += 1
            continue
        if want_tool == "weather" and call.place != want_place:
            print(f"  WRONG PLACE: {text!r} → {call.place!r}, wanted {want_place!r}")
            failures += 1
    for text in MUST_NOT_FIRE:
        call = tools.route(text)
        if call is not None:
            print(f"  FALSE FIRE: {text!r} → {call.tool} {call.place!r}")
            failures += 1
    total = len(MUST_FIRE) + len(MUST_NOT_FIRE)
    print(f"router: {total - failures}/{total} correct ({len(MUST_FIRE)} fire, "
          f"{len(MUST_NOT_FIRE)} must-not)")
    for text, note in KNOWN_IMPRECISE:
        call = tools.route(text)
        got = f"{call.tool} {call.place!r}" if call else "no tool"
        print(f"  known: {text!r} → {got}  ({note})")
    return failures


def check_recorded_parsing() -> int:
    """Parse the real recorded Open-Meteo pair — the shape that actually arrives."""
    failures = 0
    geocode = _load(RECORDED_GEOCODE)
    forecast = _load(RECORDED_FORECAST)

    spot = tools._pick_place(geocode["results"], "Frankfort", "kentucky")
    label = tools._place_label(spot)
    if label != "Frankfort, Kentucky":
        print(f"  recorded geocode picked {label!r}, wanted 'Frankfort, Kentucky'")
        failures += 1

    spoken, temp, condition = tools._summarize(forecast, "Frankfort", "fahrenheit")
    for want in ("degrees", " in Frankfort.", "High", "low"):
        if want not in spoken:
            print(f"  recorded forecast sentence missing {want!r}: {spoken!r}")
            failures += 1
    print(f"recorded pair → {label} — {round(temp)}°F, {condition}")
    print(f"  sentence: {spoken}")
    return failures


def check_summarize_fixtures() -> int:
    """Exact wording on synthetic payloads — deterministic, no network."""
    failures = 0

    def expect(got: str, want: str, label: str) -> None:
        nonlocal failures
        if got != want:
            print(f"  {label}: got {got!r}, wanted {want!r}")
            failures += 1

    clear = {
        "current": {"temperature_2m": 42.3, "weather_code": 0},
        "daily": {"temperature_2m_max": [71.2], "temperature_2m_min": [38.4],
                  "precipitation_probability_max": [10]},
    }
    spoken, temp, condition = tools._summarize(clear, "Frankfort", "fahrenheit")
    expect(spoken, "It's 42 degrees and clear in Frankfort. High 71, low 38, no rain expected.",
           "clear day")
    expect(condition, "clear", "condition word")
    if round(temp) != 42:
        print(f"  temperature: {temp}")
        failures += 1

    rainy = {
        "current": {"temperature_2m": 48.0, "weather_code": 61},
        "daily": {"temperature_2m_max": [52.0], "temperature_2m_min": [45.0],
                  "precipitation_probability_max": [80]},
    }
    spoken, _, condition = tools._summarize(rainy, "Lexington", "fahrenheit")
    expect(spoken, "It's 48 degrees and light rain in Lexington. High 52, low 45, "
                   "rain likely (80 percent).", "rainy day")
    expect(condition, "light rain", "rain condition")

    celsius = {"current": {"temperature_2m": 5.6, "weather_code": 3}, "daily": {}}
    spoken, _, _ = tools._summarize(celsius, "Paris", "celsius")
    expect(spoken, "It's 6 degrees Celsius and overcast in Paris.", "celsius, no daily")

    mystery = {"current": {"temperature_2m": 50.0, "weather_code": 42}, "daily": {}}
    spoken, _, condition = tools._summarize(mystery, "Springfield", "fahrenheit")
    expect(condition, "mixed conditions", "unknown code")

    try:
        tools._summarize({"daily": {}}, "Nowhere", "fahrenheit")
        print("  a payload without a current temperature must raise")
        failures += 1
    except tools.ToolError:
        pass

    url = tools._forecast_url(38.2, -84.87, "celsius")
    if "temperature_unit=celsius" not in url:
        print(f"  celsius URL missing the unit: {url}")
        failures += 1
    if not url.startswith(tools.FORECAST_URL):
        print(f"  URL must hit Open-Meteo: {url}")
        failures += 1

    print(f"fixtures: {'ok' if not failures else f'{failures} problem(s)'}")
    return failures


def check_failures() -> int:
    """Failure paths built on *real* sockets and real exception mapping."""
    failures = 0

    def expect_tool_error(url: str, timeout: float, needle: str, label: str) -> None:
        nonlocal failures
        started = time.monotonic()
        try:
            tools._get_json(url, timeout)
            print(f"  {label}: no ToolError raised")
            failures += 1
        except tools.ToolError as exc:
            if needle not in str(exc):
                print(f"  {label}: {exc!r} missing {needle!r}")
                failures += 1
            print(f"  {label}: {exc} ({time.monotonic() - started:.1f}s)")
        except Exception as exc:  # noqa: BLE001 — anything else is a mapping bug
            print(f"  {label}: wrong exception {exc.__class__.__name__}: {exc}")
            failures += 1

    # A server that accepts but never answers → the timeout path, for real.
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    port = silent.getsockname()[1]
    held: list[socket.socket] = []

    def hold_open() -> None:
        conn, _ = silent.accept()
        held.append(conn)  # keep it referenced: GC would close it and fake a refusal
        time.sleep(10)

    threading.Thread(target=hold_open, daemon=True).start()
    expect_tool_error(f"http://127.0.0.1:{port}/", 0.6, "didn't answer in time", "timeout")
    silent.close()
    for conn in held:
        conn.close()

    # A closed port → connection refused.
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()
    expect_tool_error(f"http://127.0.0.1:{dead_port}/", 0.6, "couldn't reach Open-Meteo", "refused")

    # An HTTP error response → the status path.
    class Broken(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"down")

        def log_message(self, *args):  # silence the test server
            pass

    broken = HTTPServer(("127.0.0.1", 0), Broken)
    threading.Thread(target=broken.serve_forever, daemon=True).start()
    expect_tool_error(f"http://127.0.0.1:{broken.server_port}/", 0.6, "HTTP 503", "http error")
    broken.shutdown()
    broken.server_close()

    # A valid reply with no results → "no place named …".
    real_get_json = tools._get_json
    tools._get_json = lambda url, timeout=5.0: {"results": []}
    try:
        result = tools.weather_result("Narnia")
    finally:
        tools._get_json = real_get_json
    if result.ok or "Narnia" not in result.note or "FAILED" not in result.context:
        print(f"  bad place: unexpected result {result}")
        failures += 1
    else:
        print(f"  bad place: {result.note}")

    # A network failure composed end-to-end → honest context, never a guess.
    tools._get_json = lambda url, timeout=5.0: (_ for _ in ()).throw(
        tools.ToolError("Open-Meteo didn't answer in time")
    )
    try:
        result = tools.weather_result(tools.DEFAULT_PLACE)
    finally:
        tools._get_json = real_get_json
    if result.ok:
        print("  composed failure must not be ok")
        failures += 1
    for want in ("FAILED", "couldn't get the weather", "Do not guess"):
        if want not in result.context:
            print(f"  composed failure context missing {want!r}")
            failures += 1
    print(f"  composed failure: {result.note}")

    # The clock is deterministic, injected-time testable, and never network.
    from datetime import datetime
    fixed = datetime(2026, 10, 19, 20, 42).astimezone()
    clock = tools.clock_result(now=fixed)
    if not clock.ok or "8:42 PM" not in clock.spoken or "Monday, October 19, 2026" not in clock.spoken:
        print(f"  clock: unexpected {clock.spoken!r}")
        failures += 1
    if "this PC's local time" not in clock.context:
        print("  clock context must say it is the PC's clock")
        failures += 1
    print(f"  clock: {clock.note}")

    # The local log round-trips ok and failure entries.
    with tempfile.TemporaryDirectory(prefix="jarvis-tools-check-") as tmp:
        log_path = Path(tmp) / "appdata" / "tools_log.jsonl"
        tools.log_result(log_path, clock)
        tools.log_result(log_path, result)
        lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        if len(lines) != 2 or lines[0]["tool"] != "clock" or lines[0]["ok"] is not True:
            print(f"  log: unexpected {lines}")
            failures += 1
        if lines[1]["ok"] is not False or "didn't answer in time" not in lines[1]["reason"]:
            print(f"  log: failure reason missing {lines[1]}")
            failures += 1
        print(f"  log: {len(lines)} lines round-tripped ({lines[1]['reason']!r})")

    return failures


def check_offline_composition() -> int:
    """weather_result end-to-end with the recorded pair — unit + context + note."""
    failures = 0
    geocode = _load(RECORDED_GEOCODE)
    celsius_payload = {
        "current": {"temperature_2m": 6.7, "weather_code": 2},
        "daily": {"temperature_2m_max": [11.0], "temperature_2m_min": [3.0],
                  "precipitation_probability_max": [30]},
    }

    def fake_get_json(url: str, timeout: float = 5.0) -> dict:
        if url.startswith(tools.GEOCODE_URL):
            return geocode
        if url.startswith(tools.FORECAST_URL):
            return celsius_payload
        raise AssertionError(f"unexpected URL {url}")

    real_get_json = tools._get_json
    tools._get_json = fake_get_json
    try:
        result = tools.weather_result("Frankfort, Kentucky", unit="celsius")
    finally:
        tools._get_json = real_get_json

    if not result.ok or result.place != "Frankfort, Kentucky":
        print(f"  composition: unexpected {result}")
        failures += 1
    if "°C" not in result.note or "partly cloudy" not in result.note:
        print(f"  composition: note must carry °C + condition: {result.note!r}")
        failures += 1
    if not result.context.startswith("Tool result (weather, Frankfort, Kentucky):"):
        print(f"  composition: context header wrong: {result.context!r}")
        failures += 1
    if "never say you cannot check the weather" not in result.context:
        print("  composition: context must pin the model to the result")
        failures += 1
    print(f"  composition: {result.note}")
    return failures


def check_geocode_discipline() -> int:
    """The Frankfurt lesson (Brandon's live catch, 2026-10-19): a query that
    names a state is a promise. "Frankfurt, Kentucky" - the spelling the
    speech-to-text produces - must resolve to Frankfort, Kentucky, never to
    Frankfurt, Germany; and a hinted search with no in-state match must fail
    honestly instead of wandering continents."""
    failures = 0
    frankfort_results = _load(RECORDED_GEOCODE)  # the real name=Frankfort reply
    german_results = {
        "results": [
            {"name": "Frankfurt am Main", "admin1": "Hesse", "country": "Germany",
             "country_code": "DE", "feature_code": "PPLA3", "population": 650000,
             "latitude": 50.11092, "longitude": 8.68213},
            {"name": "Frankfurt", "admin1": "Bavaria", "country": "Germany",
             "country_code": "DE", "feature_code": "PPLL",
             "latitude": 49.85, "longitude": 10.15},
        ]
    }

    def fake_get_json(url: str, timeout: float = 5.0) -> dict:
        if not url.startswith(tools.GEOCODE_URL):
            raise AssertionError(f"unexpected URL {url}")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        name = (query.get("name") or [""])[0]
        if name == "Frankfurt":
            return german_results
        if name == "Frankfort":
            return frankfort_results
        return {"results": []}

    real_get_json = tools._get_json
    tools._get_json = fake_get_json
    try:
        spot = tools._geocode("Frankfurt, Kentucky", 5.0)
        if spot.get("name") != "Frankfort" or spot.get("admin1") != "Kentucky":
            print(f"  geocode: 'Frankfurt, Kentucky' -> {spot.get('name')}, {spot.get('admin1')}")
            failures += 1
        spot = tools._geocode("Frankfurt Kentucky", 5.0)
        if spot.get("name") != "Frankfort" or spot.get("admin1") != "Kentucky":
            print(f"  geocode: 'Frankfurt Kentucky' -> {spot.get('name')}, {spot.get('admin1')}")
            failures += 1
        spot = tools._geocode("Frankfurt", 5.0)
        if spot.get("country_code") != "DE":
            print(f"  geocode: bare 'Frankfurt' should stay German, got {spot.get('name')}")
            failures += 1
        try:
            tools._geocode("Frankfurt, Narnia", 5.0)
            print("  geocode: 'Frankfurt, Narnia' must fail honestly, not return Germany")
            failures += 1
        except tools.ToolError:
            pass
    finally:
        tools._get_json = real_get_json
    print("  geocode discipline: state-named queries stay in-state; STT spelling resolves")
    return failures


def run_live(record: bool, timeout: float) -> int:
    """One real round trip per endpoint — the evidence run, not a habit."""
    failures = 0
    print("--- live: default place ---")
    result = tools.weather_result(tools.DEFAULT_PLACE, timeout=timeout)
    print(f"  ok={result.ok} note={result.note}")
    print(f"  spoken: {result.spoken}")
    if not result.ok:
        failures += 1

    print("--- live: named place (New York City) ---")
    result = tools.weather_result("New York City", timeout=timeout)
    print(f"  ok={result.ok} note={result.note}")
    print(f"  spoken: {result.spoken}")
    if not result.ok or "New York" not in result.place:
        failures += 1

    print("--- live: unknown place (honest failure) ---")
    result = tools.weather_result("Xyzzyville", timeout=timeout)
    print(f"  ok={result.ok} note={result.note}")
    if result.ok:
        failures += 1

    print("--- live: clock ---")
    print(f"  {tools.clock_result().note}")

    if record:
        geocode = tools._get_json(
            tools.GEOCODE_URL + "?"
            + urllib.parse.urlencode({"name": "Frankfort", "count": 10, "language": "en", "format": "json"}),
            timeout,
        )
        forecast = tools._get_json(tools._forecast_url(38.20091, -84.87328, "fahrenheit"), timeout)
        RECORDED_GEOCODE.write_text(json.dumps(geocode, indent=1, sort_keys=True) + "\n",
                                    encoding="utf-8")
        RECORDED_FORECAST.write_text(json.dumps(forecast, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8")
        print(f"  recorded → {RECORDED_GEOCODE.name}, {RECORDED_FORECAST.name}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also hit the real Open-Meteo API")
    parser.add_argument("--record", action="store_true",
                        help="refresh tests/data/ from the live API (implies --live)")
    parser.add_argument("--timeout", type=float, default=tools.TIMEOUT_SECONDS,
                        help="per-request timeout for live calls")
    args = parser.parse_args()

    failures = 0
    print("--- router ---")
    failures += check_router()
    print("--- recorded Open-Meteo response pair ---")
    failures += check_recorded_parsing()
    print("--- synthetic fixtures ---")
    failures += check_summarize_fixtures()
    print("--- failure paths (local sockets, real mapping) ---")
    failures += check_failures()
    print("--- offline composition (recorded pair + patched transport) ---")
    failures += check_offline_composition()
    print("--- geocode discipline (the Frankfurt lesson) ---")
    failures += check_geocode_discipline()

    if args.live or args.record:
        failures += run_live(args.record, args.timeout)

    if failures:
        print(f"FAILED: {failures} problem(s)")
        return 1
    print("tools_check ok" + (" (incl. live)" if (args.live or args.record) else " (offline)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Jarvis Desktop

**Jarvis on the PC** — a desktop port of the Portal voice assistant
(`com.portal.assistant`), running on Brandon's Linux desktop and powered by the
**local llama.cpp models** instead of Google Gemini.

This is a true port, built against the original as the reference:

- Upstream project: **[rudysev/portal-apps](https://github.com/rudysev/portal-apps)** by
  **Rudy ("linuxbarista")** — the workspace that holds
  [`portal-assistant`](https://github.com/rudysev/portal-assistant) (**Jarvis**),
  [`portal-wake`](https://github.com/rudysev/portal-wake) and
  [`portal-commons`](https://github.com/rudysev/portal-commons).
- The LAN voice-host protocol and the host-side pipeline are documented from
  [`rudysev/host-assistant`](https://github.com/rudysev/host-assistant).

The upstream project made this possible; full credit to its author. Personal use only —
attribution kept, upstream code is not redistributed here (the `reference/` checkouts are
the upstream repos, cloned for reference and git-ignored).

## Download

- The green **Code** button above → **Download ZIP**, or
- the pre-made [jarvis-desktop.zip](jarvis-desktop.zip) kept in this repo
  (same contents, for convenience).

Everything is committed as plain files — no archives to unpack before
you can read the code or open a pull request.

## What it is

- The same look and feel as the Portal Jarvis: always-dark warm-orange screen, clock,
  "How can I help?", suggestion chips, the big mic button.
- A **text chat screen** with streaming replies from the selected local model.
- **Spoken replies** (Phase 3): local Kokoro TTS — the full official
  `kokoro-multi-lang-v1_0` model (54 voices) in this project's `models/` folder, **bm_george**
  by default. The **Settings** dialog (top right) picks the voice — read from the installed
  model itself, by name, never by number — plus speed, an on/off toggle, the output device
  (defaults to the **system default output** — on this PC that is the Moonlight path; corrected
  2026-10-19, see NOTES.md), a **Preview** line and a **Test sound**. Everything persists in
  `appdata/config.json` (assistant name, greeting, voice, theme accents — editable without
  touching code).
- **Push-to-talk speech input** (Phase 4): hold the mic button (home screen or beside the
  message box), speak, release — the words are transcribed **locally** (sherpa-onnx SenseVoice
  in `models/`, no cloud) and land in the chat exactly like typed ones, then the reply speaks
  through the Phase 3 voice path. The Settings dialog also picks the **microphone**
  ("Automatic" = the system default source). Empty or failed transcriptions are reported
  honestly — nothing is invented or half-sent.
- **Wake word “hey jarvis”** (Phase 5 + 5.1, **off by default**): turn it on in
  **Settings → Wake word** and the app listens *on this PC only* (openWakeWord, local ONNX
  models in `models/openwakeword/` — nothing leaves the machine). Every detection is
  **verified before it counts**: the app transcribes the moment with the local speech model
  and only a transcript that really contains the name (spelling wobble allowed) plays the
  chime — TV soundalikes are logged as rejected and ignored. After the chime, ask your
  question: the take ends ~1 s after you stop talking, measured against *the room's own*
  noise floor (“Hang time” and “Margin” sliders; 15 s hard cap), and goes through the same
  local transcription → reply path as push-to-talk. A header badge shows whenever the mic is
  truly being listened to, and vanishes when the feature is off; the trigger level is
  adjustable; every candidate (accepted **and** rejected) is logged to
  `appdata/wake_log.jsonl` with its verification transcript, and Settings counts the two
  separately. The listener pauses while Jarvis speaks or records (he must not wake himself);
  a configured microphone that has gone away is reported honestly — never a silent switch to
  a different mic.

Local models: **Qwen3-8B** (`buster-ai`, :11434), **Qwen2.5-Coder-14B** (`buster-14b`,
:11436), **Qwen3-Coder-30B** (`buster-coder`, :11435) — llama.cpp servers with an
OpenAI-compatible API.

## Run

```bash
./run.sh
```

Start a model first if one isn't running — the **Models** button (top right) opens the built-in
Model Switcher panel: status lights, exclusive Start (starting one stops the others, exactly
like Brandon's Model Switcher). With no model live the app says so plainly and disables Send —
it never starts a model by itself. The **Settings** button holds the voice, the audio output
and the speech-input microphone; spoken replies and speech recognition run on CPU with no
external services.

## Status

- **Phase 0 — discovery: done** (2026-10-18). See [`NOTES.md`](NOTES.md) for the full report:
  how Jarvis works, the exact local-server protocol, the wake hand-off, this PC's inventory and
  the gap analysis + architecture.
- **Phase 1 — chat app: done** (2026-10-18) — Portal-style window (clock, "How can I help?",
  suggestion chips, mic button), streaming multi-turn chat from the local models, model picker
  with live detection, state that survives restarts. Details + acceptance evidence in NOTES.md.
- **Phase 2 — Model Switcher panel: done** (2026-10-18) — the built-in panel (Models button):
  status lights, exclusive Start/Stop using the exact `sudo -n systemctl` mechanism, never
  auto-starts. Verified against the real services; details + evidence in NOTES.md.
- **Phase 3 — voice output (Kokoro) + Settings: done** (2026-10-18) — the full 54-voice
  `kokoro-multi-lang-v1_0` downloaded into `models/` and made the app's TTS model; bm_george by
  name; Settings dialog (voice/speed/on-off/output device, Preview, Test sound); data-driven
  catalog straight from the installed model; replies spoken sentence-by-sentence while they
  stream. Evidence in NOTES.md (offscreen smoke, real synthesis checks, live audible run).
- **Phase 4 — push-to-talk STT + audio routing fix: done** (2026-10-19) — hold-to-talk on the
  home mic and a compact mic in the input bar; local **sherpa-onnx SenseVoice** transcription
  (int8, in `models/sense-voice/`) on a worker thread; transcript enters the chat like typed
  words; mic picker in Settings; audio auto-pick corrected to follow the **system default
  output** (the Phase 3 analog-jack miss — see NOTES.md). Wake-word feasibility report included
  there (report only). Evidence: offscreen smoke, round-trip text→voice→text check, real
  microphone capture + decode.
- **Phase 5 — “hey jarvis” wake word: done** (2026-10-19) — opt-in always-listening via
  **openWakeWord** (local ONNX models loaded from `models/openwakeword/`), wake = chime →
  auto-stopping recording (energy VAD, ~1 s silence / 15 s cap) → SenseVoice → the same reply
  path as push-to-talk; listener on its own worker thread with honest header states
  (off / starting / listening / paused / error), suppression while speaking or recording plus
  short cool-downs, wake log + count in Settings. Evidence: extended offscreen smoke (wiring,
  suppression, cancel, healing — listener stubbed), synthetic-voice detection check (Kokoro
  saying “hey jarvis” fires on three voices; silence and speech negatives ≈ 0), measured idle
  CPU/memory, live-mic capture + VAD checks. Honest limits and numbers in NOTES.md — the
  real-voice acceptance test is Brandon's.
- **Phase 5.1 — wake-word soak fixes: done** (2026-10-19) — two defects from Brandon's live
  soak, both fixed. (1) End-of-speech is now relative to the room's own measured noise floor
  (a trailing ~10 s low-percentile estimate, seeded from the wake listener) instead of a fixed
  dBFS clamp that sat *under* his ≈ −19 dBFS TV/speaker bed — takes end ~1 s after he stops
  instead of riding to the cap; “Hang time” (0.5–3.0 s) and “Margin” (4–16 dB) are healed
  config keys with Settings sliders. (2) A detection is a *candidate* until verified: the
  listener keeps the last ~3.5 s of mic audio, the app transcribes the moment with the local
  SenseVoice model, and only a transcript that really contains the name (one-edit wobble
  allowed) chimes and records — TV lookalikes are rejected silently and logged (score +
  transcript + accepted/rejected) to `appdata/wake_log.jsonl`. Evidence: extended smoke;
  wake_check with a loud-bed before/after (old math rides, new stops 0.7 s after the speech),
  old-vs-new on real captured room audio, listener integration over a synthetic stream, name
  cases, verification latency (≈ 0.2 s warm, ≈ 1.4 s first-ever), all other suites green.
  Numbers and honest limits in NOTES.md.

## Layout

```
run.sh        # one-command launch
app/          # the application (Python + PyQt6)
models/       # downloaded models (TTS + STT + wake word) — git-ignored, fetched on demand
tests/        # dev checks: offscreen UI smoke test, voice check, speech-input check, wake-word check, live streaming check
appdata/      # saved state + config (git-ignored)
reference/    # upstream clones for reference (git-ignored)
NOTES.md      # dated notes, decisions, discovery report
```

## Credits

Portal-Assistant ("Jarvis"), portal-wake and portal-commons are community projects by
[rudysev](https://github.com/rudysev) — **not affiliated with, endorsed by, or sponsored
by Meta or Google**. "Meta Portal" and "Portal" are Meta trademarks; "Gemini" is a Google
trademark, used only to identify compatible hardware/services. This port is personal,
non-commercial use with attribution.

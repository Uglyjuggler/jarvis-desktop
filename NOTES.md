# Jarvis Desktop — Notes

Port of the Portal **Jarvis** assistant (`com.portal.assistant`, from
[rudysev/portal-apps](https://github.com/rudysev/portal-apps), by Rudy / "linuxbarista") to
Brandon's Linux desktop, powered by the three local llama.cpp models instead of Google Gemini.
Personal use, upstream attribution kept (see README.md).

All references below are to files inside `reference/` (upstream checkouts, git-ignored here).

---

## 2026-10-18 — Phase 0: Discovery

### Sources cloned

| Repo | Commit | What it is |
|---|---|---|
| rudysev/portal-apps | `01ce817` | workspace superproject |
| └ portal-assistant (Jarvis) | `4b50bfd` | the assistant app (`com.portal.assistant`) |
| └ portal-commons | `8dc290a` | shared Kotlin mic/logging library |
| └ portal-wake | `4bbe80c` | "hey jarvis" wake listener (`com.portal.wake`) |
| rudysev/host-assistant | `776306c` (2026-07-10) | the LAN **voice host** — server side of Jarvis's "local server" backend (Python, MIT). Not pinned by portal-apps; found via the GitHub profile, cloned as reference. |

License note as observed: host-assistant ships MIT; portal-commons README says MIT; portal-assistant
and portal-wake carry no LICENSE file in the checkout (portal-assistant ships only the Inter font's
OFL license). This port is personal use with full attribution; we don't redistribute their code.

### 1. How Jarvis is put together

**What it is:** a hands-free voice assistant for Meta Portal (gen1 Android 9 / gen2 Android 10).
Background voice is the primary mode — the answer plays through the speaker with only a small
orange bar over whatever is on screen; opening the app shows a live on-screen chat of the same
conversation. One conversation can be multi-turn; it ends on its own when the user stops talking.

**Architecture (pure reducer + thin I/O shell):**

- `conversation/ConversationState.kt` — pure state machine: `CONNECTING → LISTENING → SPEAKING →
  (LISTENING again | ENDED)`. Turn end requires all three: server `turn_complete` **and** audio
  playback drained **and** no tool calls in flight. Exactly two client timers: a 5 s *no-speech*
  timer (release mic when the user goes quiet, with a local-energy grace so a late follow-up isn't
  dropped) and a 30 s *stall* watchdog (dead air = nothing playing, no tools in flight).
- `conversation/AssistantEngine.kt` — the impure shell: mic capture (16 kHz mono PCM), backend
  socket, 24 kHz audio playback, the reducer and timers, all on one Handler thread. Half-duplex:
  the mic is muted while the model speaks.
- `conversation/backend/VoiceBackend.kt` — **the swappable seam**. Interface: `connect` /
  `sendAudio` (16 kHz PCM in) / `sendText` / `sendToolResponse` / `close` + a 12-callback listener
  (ready, transcripts, audio, model-generating, turn-complete, interrupted, tool calls, error,
  closed). `Backends.resolve()` picks the concrete backend from Settings: **Gemini Live** (cloud,
  default) or **LocalBackend** (LAN voice host).
- `conversation/SessionPrompt.kt` — the system instruction for a session, in layers:
  1. `SystemPrompt.kt` role: *"Warm, friendly display voice assistant. Never ask the user to say a
     wake word or goodbye (conversations end automatically)."* + tool-usage rules for the
     device tools (time, timers, volume, brightness, DND, music, app launch).
  2. Enabled external tool-provider bullets.
  3. `SystemContext` — device clock/timezone + approximate location, so "what time is it" /
     "what's the weather" work without the user stating where/when they are.
  4. On tap-to-talk resume: recent transcript history (≤4 000 chars) so the mic continues the
     on-screen conversation.

**UI flow (Compose, `ui/ConversationScreen.kt` + `theme/`):**

- Always-dark, warm-orange identity (not assistant blue): accent `#FF8A00` (matches the recording
  bar), background ink `#0A0A0C` with a slow warm radial glow, user-turn chip `#3A332B`, body text
  `#F1ECE6`, secondary text at 0.80 alpha, bundled **Inter** typeface.
- **Idle home**: ambient clock + date on the left; on the right the greeting **"How can I help?"**
  and a short row of **static suggestion chips** — the stock pair is **"What's the weather?"** and
  **"Set a 10-minute timer"** (plus, when external providers are on, e.g. "Turn off the lights").
  A chip **sends its text as the first turn** (not just opens the mic). Bottom center: big round
  orange **mic button** with "Tap or say 'Hey Jarvis'". Header: app orb, "Jarvis", model subtitle
  (e.g. "Gemini 2.5" / "Gemini 2.5 · Speaking…"), gear (Settings) and "+" (new session) at top right.
- **Connecting / listening** (nothing said yet): center-stage big word "Connecting…"/"Listening…"
  over a reactive orb (pulse while connecting, mic-level scaling while listening).
- **Transcript**: bottom-anchored; user turns as small monospace chips with a "›" prefix, model
  answers as large light text revealed **word-by-word in step with the audio actually played**
  (then snaps to full text). Model markdown is stripped to plain prose deliberately (it's a spoken
  answer). Auto-scroll with pause-on-scroll-up + scroll-to-bottom button; "thinking" dots fill the
  tool/search gap; active timer countdown cards; dismissible notice banner for failures.
- **Settings**: model picker, default music app, location override, external tools, API key —
  all hidden in Local mode except what applies (the **host owns the model** in local mode).

**Conversation loop (local mode):** open `wss://host:port` → send `setup` (system prompt + tool
declarations) → host replies `ready` → stream 16 kHz mic PCM binary frames → host runs VAD/STT/LLM/
TTS → sends back `input_transcript` deltas, `model_generating`, 24 kHz PCM frames,
`output_transcript` deltas, `turn_complete` → engine plays audio, reveals words, then re-opens the
mic for a follow-up (multi-turn) until the no-speech timer ends the session.

### 2. The local server backend — exact protocol (as implemented)

Client: `portal-assistant/.../conversation/backend/local/LocalBackend.kt`.
Server reference: `host-assistant/host_assistant/protocol.py` + `pipeline/` + `__main__.py`.
"A voice host on your LAN" — **the host does everything Gemini's server did**: VAD, ASR, LLM with
tool-calling, TTS, and web-search grounding. The app just ships audio and control frames.

**Transport**

- One WebSocket per conversation, **`wss://host:port`** — no path, no query. Settings accepts
  `host:port` (e.g. `192.168.1.5:8080`) or explicit `wss://host:port`; normalized to `wss://…`.
  Input is rejected unless the host is private/LAN-looking (10/8, 172.16-31, 192.168/16, 127/8,
  169.254/16, `localhost`, `*.local`, or a short mDNS name). **Port is required.**
- TLS: **self-signed trusted blindly** (shipping default, `TRUST_SELF_SIGNED`); a `SYSTEM` mode
  exists for a CA-issued cert (unused). The host generates its cert on first boot
  (`~/.host-assistant/tls/`, openssl).
- **No auth of any kind.** The "credential" is the host URL; the host's model is not chosen by
  the app (model picker + API key are hidden in Local mode).
- OkHttp client: 20 s ping, 15 s connect timeout, no read timeout (socket stays open).
- One WebSocket client at a time (host-side restriction).

**App → host**

- Thin binary WS frame = one raw PCM chunk, **16 kHz mono 16-bit little-endian** (mic audio).
  Sent only after `setup` is acknowledged (`ready`); no base64, it's a LAN socket.
- Text WS frame (JSON), three kinds:
  - `{"type":"setup","systemPrompt":"…","tools":[…]}` — sent **first, on socket open**. Tools are
    the tool declarations normalized to standard JSON Schema (OpenAPI-subset on the app side).
  - `{"type":"user_text","text":"…"}` — one complete text turn (a tapped suggestion chip); the
    host answers immediately without waiting on VAD.
  - `{"type":"tool_result","results":[{"id":"…","name":"…","response":{…}}, …]}` — ping-pong
    answers to host tool calls; **order must match** the original `tool_call` order.

**Host → app**

- Thin binary WS frame = one raw PCM chunk, **24 kHz mono 16-bit LE** (TTS audio).
  The first binary frame implicitly means "the model is generating" if no `model_generating` was
  seen (client safety net).
- Text WS frame (JSON), keyed by `type`:
  - `ready` — setup accepted; the app may start streaming.
  - `input_transcript` `{text}` — **delta** of what the user said (new text since last frame).
  - `output_transcript` `{text}` — **delta** of the model's spoken answer.
  - `model_generating` — model started producing (host should emit before first audio).
  - `turn_complete` — the model's turn is done.
  - `interrupted` — the model's output was interrupted/revised (client flushes queued audio).
  - `tool_call` `{calls:[{"id","name","args":{…}}]}` — the model wants a tool that runs on the
    app (or in our port: a tool the desktop app must answer).
  - `tool_cancel` `{ids:[…]}`.
  - `error` `{message}`.
  - Unknown types / unparseable JSON are ignored.

**Who executes tools:** the host has an explicit allowlist (`web_search`, `get_weather` — Open-Meteo
for weather, Tavily or DuckDuckGo for search, no Google dependency). Every other tool declared in
`setup` is forwarded to the app via `tool_call` and back via `tool_result` (8 s host-side timeout).

**STT/TTS in local mode:** both are **host-side** — the app never runs speech recognition or
synthesis itself. Reference host stack (Python 3.12, Pipecat 1.5.0): **Silero VAD** ends the turn
(stop_secs default 0.8) → **Whisper** STT (WhisperSTTServiceMLX — Apple-Silicon-only today; the
README says swap for Pipecat's faster-whisper path on Linux) → **Qwen3.5 via Ollama**
(OpenAI-compatible API) for the LLM → **Kokoro TTS** (kokoro-onnx; default voice `af_heart`) →
24 kHz PCM out. The host prepends `/no_think` and appends "keep every spoken reply to at most
three short sentences" to the app's system prompt. Optional warm-ups preload Whisper/Kokoro/Ollama.

**Relevant for the desktop port:** our app is *both* the UI and the "host" (it talks straight to
llama.cpp), so for us: chat = `POST /v1/chat/completions` with `stream:true`; TTS/STT are things
we add ourselves in Phases 3/4 (see gap analysis). The wss protocol above is documented because it
is Jarvis's real seam — and it's the basis of the v2 idea to make this PC a genuine voice host for
Brandon's actual Portal (see v2 candidates).

### 3. How portal-wake hands off to Jarvis

- `portal-wake` (`com.portal.wake`) is a **headless, always-on** Vosk wake-word listener that owns
  the microphone. Wake words are **runtime plugins**, not hard-coded: any app declares an exported
  receiver with intent-filter action `com.portal.wake.action.WAKE` and named meta-data —
  `com.portal.wake.phrase` (required; the **last word is the keyword**, the word before it the
  lead, so "hey jarvis" works and "hey" lives in the plugin's phrase), `…min_confidence`
  (optional; Jarvis declares **0.6**), `…id` (optional, defaults to the keyword).
- **On a match** portal-wake **releases the mic** and sends an **explicit broadcast**
  `ACTION_WAKE` to that receiver with `com.portal.wake.extra.ID` = matched id. Jarvis's
  `WakeHandoffReceiver` just starts `AssistantService` to take the freed mic.
- **No "done" signal:** the handler never says when it's finished. Portal-wake reclaims by
  *detecting* that recording stopped (`AudioManager.AudioRecordingCallback`), and it *yields* when
  it sees a foreign recording session (e.g. a foreground tap in Jarvis) — by detection, no
  broadcast, no timers. It also stands down for phone/VoIP calls.
- Gen 2 (Android 10) note: a background sideloaded mic is silenced by the OS; hands-free there
  only works while Jarvis is on screen (Jarvis runs its own foreground Vosk detector on gen2, via
  the same shared `WakeMicEngine` in portal-commons). Gen 1 always works via portal-wake.

**Desktop relevance (Phase 4):** there is no Android here; the investigation target is an
always-on "Hey Jarvis" (e.g. openWakeWord) with a mic the Fifine provides — cost/accuracy report
only, no build, per the job.

### 4. This PC — inventory (2026-10-18)

#### 4a. Model services (systemd SYSTEM units in /etc/systemd/system)

llama.cpp build `b11102-5-gc350a40bb` (2026-09-22), Vulkan backend, GPU = AMD RX 580 8 GB
(one model fits at a time). All units: **`Restart=no`, `disabled`** (boot-off), started only by
hand or the Model Switcher. No `--api-key` anywhere → **no auth**.

| Unit | Model (GGUF quant Q4_K_M unless noted) | Port | Bind | Context | Flags |
|---|---|---|---|---|---|
| `buster-ai` | Qwen3-8B | 11434 | `0.0.0.0` | 32768 | `--jinja --reasoning off --reasoning-budget 0` |
| `buster-14b` | Qwen2.5-Coder-14B-Instruct | 11436 | `0.0.0.0` | 16384 | `--jinja` |
| `buster-coder` | Qwen3-Coder-30B-A3B | 11435 | `127.0.0.1` | 32768 | `--jinja`, unit also has `Conflicts=buster-ai` + `Before=buster-ai` |
| (`sd-image`) | FLUX image generator — *not part of this port v1* | (1234) | 127.0.0.1 | — | in the Model Switcher's list, out of scope |

**API surface** (verified against the installed source `~/llama.cpp/tools/server/server.cpp`):
OpenAI-compatible **`POST /v1/chat/completions`** (also `/chat/completions`) with **`stream:true`
SSE** (`data:` chunks, terminated by `data: [DONE]`) — exactly the pattern
`model-switcher/model-chat.py` already uses. Also `GET /health` (public, no auth; 200 when the
model is loaded — poll target), `GET /v1/models`, `GET /props`, `POST /completion` (legacy),
`/tokenize`, `/props`, `/slots`, and `POST /v1/audio/transcriptions` (only useful with an
audio-capable model — our three are text-only, so no STT this way). `--jinja` means the server
side handles chat templates (Qwen3 chat template); tool-calling is possible via templates but we
don't need it in v1.

#### 4b. How the Model Switcher controls the units (`/mnt/PROJECTS/model-switcher/`)

PyQt6 GUI (`model-switcher.py`, v4). Mechanism, exactly as implemented:

- All systemctl calls go through **`sudo -n systemctl …`** (passwordless, 30 s timeout);
  `sudo -n` fails cleanly if the rule ever goes away. This is the *only* sudo used.
- Status: `sudo -n systemctl is-active <unit>` (parsed: active / activating / inactive / failed),
  polled by a 2 s `QTimer`; plus amdgpu sysfs VRAM/busy readout.
- **Start** (exclusivity): for every other unit in `[buster-ai, buster-14b, buster-coder,
  sd-image]` — `stop` it if active/activating — then `reset-failed <unit>`, then `start <unit>`.
- **Stop**: `stop <unit>` then `reset-failed <unit>`. "Stop everything" = stop + reset-failed for
  all four.
- **Models stay OFF unless Brandon starts one**: nothing autostarts (all units disabled +
  `Restart=no`); the switcher never starts anything on launch.
- "Prompt" opens a gnome-terminal running `model-chat.py`, which polls `/health` until the model
  answers (up to 300 s) then streams replies from `/v1/chat/completions`.
- Status lights: ● green RUNNING / amber WAKING UP… / red FAILED (try Stop, then Start) /
  grey OFF; Start disabled unless off/failed, Stop disabled unless active/activating.
- Row copy (use as the basis for Phase 2's plain descriptions/sizes):
  - **Qwen3-8B** — "Everyday chat. Chat, questions, light coding help. The quickest of the three
    to wake up." 4.7 GB disk · ~5 GB VRAM.
  - **Qwen2.5-Coder-14B** — "Mid-size coder. Sharper code help than the 8B, slower to wake,
    shorter memory (16K context)." 8.4 GB disk · most of the VRAM.
  - **Qwen3-Coder-30B** — "The big brain. Best coding and reasoning of the three. Slowest to
    wake — spills into system RAM by design." 18 GB disk · VRAM + system RAM.

#### 4c. TTS available

- **Kokoro via sherpa-onnx — the Blaze News Desk stack** (`news-anchor/render_briefing.py`):
  `sherpa_onnx` Python **1.13.8** (system python3.12), model dir
  **`/home/uglyjuggler/Downloads/sherpa-onnx/kokoro-en-v0_19`** (model.onnx 330 MB, voices.bin,
  tokens.txt, espeak-ng-data; 24 kHz native). Blaze uses **sid=5 (af_kore)**; guests sid=1.
  Text is chunked and synthesized per segment; WAVs written with the stdlib `wave` module.
- Same model shipped to the Portals as the Android TTS engine (`portal-tts-kokoro/`, sherpa
  `TtsEngine` with `kokoro-en-v0_19`; its default sid=11 am_adam; voice changeable via SID).
- No piper install found. `paplay` / `pw-play` / `aplay` / `ffplay` are all available for WAV
  playback; `libportaudio.so.2` is present (so `sounddevice` is a viable later install).
  **PyQt6 here has no QtMultimedia** → don't plan on QMediaPlayer; generate WAV + `paplay` is the
  zero-dependency route. (Note: PipeWire default sink currently is `sink-sunshine-stereo`, the
  Sunshine streaming sink — playback routing decision flagged for Phase 3.)

#### 4d. STT available

**None installed** — no vosk, no whisper.cpp, no sherpa-onnx STT models on disk. Candidates for
Phase 4 (when/if Brandon green-lights): sherpa-onnx offline STT (we already have the runtime —
add e.g. a zipformer/whisper ONNX model), or whisper.cpp/faster-whisper. Mic hardware is good:
**Fifine USB mic** is the default PipeWire source (plus webcam mic and onboard analog).
Wake-word candidates (Phase 4 report only): openWakeWord (not installed).

#### 4e. Other environment facts

- Python **3.12.3** system-wide with **PyQt6 6.6.1**, PyQt5, numpy 1.26.4, requests 2.31,
  websockets 10.4, sherpa_onnx. No venvs currently. (Any new dependency goes into a venv inside
  this project folder, per the job rules.)
- Desktop session: **X11**, `DISPLAY=:0`. No Inter font installed (upstream bundles it) — use a
  system sans for v1, bundle later if Brandon wants exact type.
- Everything outside `/mnt/PROJECTS/jarvis-desktop/` treated read-only; systemd units read,
  never edited.

### 5. Gap analysis — Jarvis's local mode vs. this PC

| Jarvis local mode expects (on the host) | This PC has | What the desktop app must do |
|---|---|---|
| WebSocket voice host: VAD, STT, LLM+tools, TTS, web search | llama.cpp OpenAI-compatible HTTP chat (streaming), no VAD/STT, no tools, no search | Skip the wss host entirely for v1: talk HTTP to llama.cpp directly; supply TTS (Phase 3) and STT (Phase 4) in-app |
| LLM behind the host (Ollama / OpenAI-compatible) | 3 llama.cpp chat servers, one live at a time, no auth | In-app **model picker** + live detection via `/health` (Phase 1); start/stop + exclusivity (Phase 2) |
| Host owns the model ("model picker hides in Local") | Model choice is meaningful again | Keep the model picker visible — it's the desktop's equivalent of the portal's Settings → Backend |
| Web-grounded answers (weather/news…) | No search/Tavily key on this PC | v1: "What's the weather?" chip → simple local answer (or short model answer); real web grounding goes to v2 candidates |
| Gemini voice (native-audio, warm) | Kokoro voices on disk (af_kore used by Blaze; am_adam portal default) | Phase 3 "Jarvis" voice profile — pick the closest Kokoro voice, previewable |
| Wake: Vosk on-device (Android) | No wake engine; Fifine mic available | Phase 4 investigation (openWakeWord etc.) — report only |
| One-model exclusivity (their concern is VRAM) | Same: 8 GB RX 580, units disabled + `Restart=no` | Phase 2 mirrors the Model Switcher's exact `sudo -n systemctl` calls; **never** auto-start |

### 6. Recommended architecture for this port (what Phase 1 will build)

**Python 3.12 + PyQt6** — mirrors the Model Switcher precedent (same stack, same machine, already
installed), handles streaming and audio fine, and needs no new dependency for v1.

```
jarvis-desktop/
  run.sh                 # one-command launch
  app/
    jarvis/
      __init__.py
      main.py            # entry: QApplication + window
      state.py           # persisted state (JSON) in appdata/
      models.py          # the three models: names, units, ports, descriptions, sizes
      llm.py             # streaming chat client → llama.cpp /v1/chat/completions (SSE)
      prompt.py          # Jarvis personality/system prompt (upstream SystemPrompt + spoken style)
      switcher.py        # Phase 2: sudo -n systemctl wrapper (exact Model Switcher behavior)
      tts.py             # Phase 3: sherpa-onnx Kokoro → WAV → paplay
      stt.py             # Phase 4: STT (sherpa-onnx), push-to-talk
      ui/
        window.py        # header (Jarvis + model + status), pages
        home.py          # clock, "How can I help?", chips, mic button
        chat.py          # bubbles, streaming reveal, input + Send
        settings.py      # voice options (Phase 3), etc.
        theme.py         # Portal palette: #FF8A00 / #0A0A0C / #3A332B / #F1ECE6 + warm glow
  appdata/               # state.json (git-ignored) — survives restarts
  reference/             # upstream clones (git-ignored)
  NOTES.md  README.md  run.sh
```

- **Phase 1 chat**: `POST /v1/chat/completions` `{messages, stream:true}` with a QThread worker;
  parse `data:` SSE lines; tokens appear as they arrive. Model live-check = `GET /health`
  (200 = ready). System prompt = upstream role (*"Warm, friendly display voice assistant…"*)
  extended with "spoken-style, concise replies, no markdown/lists" + current date/time context
  (the desktop equivalent of upstream `SystemContext`). Conversation history kept in-app (multi-turn).
- **State**: `appdata/state.json` (inside the project folder per the job's containment rule) —
  selected model + settings + last transcript, so restarts keep the app's state. A "+" button
  starts a fresh session (matches Portal behavior).
- **Phase 2**: switcher panel with the Model Switcher's exact mechanism (4b above) and copy.
- **Phase 3**: TTS via sherpa-onnx Kokoro (same model dir as Blaze), WAV → `paplay`; keep
  everything inside the app; Settings for voice / speed / on-off; "Jarvis" default profile chosen
  by ear against the Portal assistant (candidates: bm_george British-male / am_adam / af_kore).
- **Phase 4**: only with Brandon's go — STT + PTT (sherpa-onnx model download into the project),
  wake-word feasibility report (no build).

**Deviation from the Portal app that is deliberate:** the Portal streams mic audio to a host that
does VAD/STT/TTS; the desktop app is self-contained (UI + models + later TTS/STT in one process).
The *seam* is preserved in spirit: small modules (`llm.py`, `tts.py`, `stt.py`) so backends stay
swappable, exactly like upstream's `VoiceBackend`.

### 7. Open questions for Brandon (answered before Phase 1 starts)

1. **Testing Phase 1 needs one model live.** The job's rules allow `sudo -n systemctl` only in
   Phase 2. Options: (a) you start one via the Model Switcher when we test, or (b) you pre-approve
   Phase 1 to use the same `sudo -n systemctl start/stop` calls the switcher makes. Which?
2. **Default "Jarvis" voice** (Phase 3 previews): British-male `bm_george` for a classic Jarvis
   feel, vs `am_adam` (the Portal TTS engine's default) vs `af_kore` (Blaze's voice)? Default
   suggestion: `bm_george`, changeable in Settings.
3. **Where should speech come out?** The PipeWire default sink right now is the *Sunshine*
   streaming sink — should Jarvis speak to the default sink, or are you streaming the desktop
   audio to somewhere and want it in the stream, or should we target specific speakers? (Phase 3
   concern; answer whenever.)
4. State file lives at `jarvis-desktop/appdata/state.json` (git-ignored, inside the project per
   the containment rule) — okay?

### v2 candidates (ideas only — not in v1 code)

- **Linux port of `host-assistant` on this PC** — implement the documented wss protocol with
  llama.cpp + sherpa-onnx (Whisper + Kokoro) instead of Ollama/MLX, so Brandon's *actual Portal
  Mini* could point Settings → Backend → Local server at this PC: same brain, same voice on both
  devices. This is the single most valuable follow-up the discovery surfaced.
- Always-on "Hey Jarvis" wake word (after Phase 4's feasibility report).
- Web grounding in-app (Open-Meteo weather / DuckDuckGo) to replace web-grounded Gemini answers.
- Timers as real countdowns (upstream has a full tool + cards UI).
- Bundle Inter + the Portal orb art for pixel-accurate identity.
- Sessions/memory persistence beyond last-transcript; "resume context" on app re-open.
- Tool-calling via llama.cpp `--jinja` templates (volume/brightness equivalents, app launch).
- Orange-bar overlay / tray mode ("background voice" behavior like the Portal).

### Decisions log

- **2026-10-18** — Build the desktop app as a self-contained Python/PyQt6 assistant that talks
  directly to llama.cpp HTTP; do **not** run the Pipecat host for v1 (it targets Apple-Silicon STT
  and adds a layer Brandon doesn't need on one machine). The wss protocol is documented for
  reference and for the v2 Portal-host idea.
- **2026-10-18** — TTS = sherpa-onnx Kokoro (already installed, same model as Blaze and the
  Portals = maximum voice continuity) rather than installing piper or a new engine.
- **2026-10-18** — State under `appdata/` in the project folder (containment rule) rather than
  `~/.config`.
- **2026-10-18** — Phase 2 will copy the Model Switcher's mechanism *exactly*: `sudo -n systemctl`
  (`is-active` polling; start = stop others → `reset-failed` → start; stop = stop +
  `reset-failed`), and the app will never start a model on its own.

---

## 2026-10-18 — Phase 1: the chat app (text first)

**Built** (`app/` — Python 3.12 + PyQt6 6.6.1, no new dependencies; `run.sh`; `tests/`):

```
app/jarvis/
  __main__.py   entry (QApplication + window)
  state.py      appdata/state.json — model, transcript, window size (atomic, best-effort)
  models.py     the three models: names/units/ports/descriptions/sizes (Model Switcher copy)
  prompt.py     Jarvis system prompt (upstream role + spoken style + date/time context)
  llm.py        streaming SSE client → llama.cpp /v1/chat/completions (worker thread)
  health.py     /health poller (3 s) → running / waking up / off per model
  ui/           theme.py · widgets.py · home.py · transcript.py · window.py
run.sh          one-command launch
tests/          smoke.py (offscreen UI test) · live_chat.py (real streaming check)
```

**The window (Portal look):** near-black ink with the warm bottom-center glow; header with the
orange orb, "Jarvis", and the model subtitle ("Qwen3-8B · Running" / "· Speaking…"); idle home with
the big clock + date, **"How can I help?"**, the two stock chips ("What's the weather?" /
"Set a 10-minute timer"), and the big orange **mic button** — present, with the caption "voice input
& spoken replies arrive in Phases 3–4", and a click explains rather than pretending. The transcript
switches in when the first turn is sent: user lines as small warm chips ("›  …"), Jarvis's answers
as large light text streaming in live (a subtle caret marks the live bubble). Input bar with a
typing box (Enter sends, Shift+Enter newline) and a Send button that shows "…" while streaming.

**Model handling (acceptance: detects what's live, graceful when nothing is):** the picker lists all
three with status dots; `/health` is polled every 3 s (200 = running, 503 = waking up, refused =
off). Send is enabled only while the *selected* model is running. On launch, if the saved model is
off and exactly one other model is running, the app quietly switches to the live one once. The
notice strip says exactly what's happening: "No model is running. Start one in the Model Switcher —
the built-in start/stop panel arrives in Phase 2", or which other model is running when the selected
one is off. **No systemctl anywhere in Phase 1 and no auto-starting** (Brandon's rule).

**Chat behavior:** system prompt = upstream `SystemPrompt.kt` role ("Warm, friendly display voice
assistant…") + spoken-style rules (one to three short sentences, plain prose, no markdown/lists) +
current date/time (upstream `SystemContext` equivalent) + an explicit *"no tools yet"* clause so it
says "can't do that yet" instead of pretending to set timers or check weather. Requests stream from
`/v1/chat/completions` (`stream: true`, `max_tokens: 512`) — the same wire `model-chat.py` proves on
this machine — in a worker thread, so the UI never blocks. History is capped (~24 k chars) before
sending; multi-turn works; answers are selectable text (copyable); markdown is deliberately shown
as plain prose (upstream strips it for the same reason — these are spoken answers).

**Chips:** when a model is live a chip sends its text as a turn (the Portal's behavior); when none
is live, clicking a chip stages the text in the typing box instead (the spec's "chips may insert
text") so the click is never lost.

**State survives restarts:** `appdata/state.json` (git-ignored, inside the project per the
containment rule) stores the selected model, the transcript (last 200 turns), and the window size;
restored on launch. **New session** clears the conversation and returns to the idle home (the
Portal's "+").

**Acceptance evidence (this session, 2026-10-18):**

- `./run.sh` launched; the X11 window "Jarvis" mapped on the desktop (`xwininfo`), with Qwen3-8B
  live — the real launch path, one command.
- `tests/smoke.py` (offscreen): passes — no-model notice + disabled Send; chip staging; transcript,
  streaming bubble, notice; live model enables Send; New session resets to idle.
- `tests/live_chat.py` → real stream from Qwen3-8B through the app's own client:
  *"Hello, I'm Jarvis, and I'm ready to help you."*
- Full window end-to-end (offscreen): sent "Say exactly: desktop check" → streamed reply
  *"Desktop check complete. All systems operational."* → transcript switched in, subtitle
  "Qwen3-8B · Running", both turns persisted to `state.json`.
- Model switching: selection, statuses, notices and routing verified with the other two models off
  (the normal state). A full switch between two different live models can't be exercised in
  Phase 1 (one GPU; starting a second model needs systemctl, which Brandon reserved for Phase 2) —
  the app simply talks to whichever model is selected *and* live.

**Decisions:** single-window design with the idle home showing while the transcript is empty
(mirrors upstream's `(turns, phase)` center states); no separate Settings dialog yet (Phase 3);
`max_tokens=512` to keep spoken answers tight; DejaVu fonts (Inter is not installed on this machine);
page-glide/ambient motion deliberately left out (a v2 nicety).

**New v2 candidates from Phase 1:** cancel/stop streaming; regenerate reply; Ctrl+N shortcut;
session naming; optional light markdown rendering; bundle Inter + the Portal orb art for exact type
identity.

---

## 2026-10-18 — Phase 2: the built-in Model Switcher panel

**Built** (`app/jarvis/switcher.py` + `app/jarvis/ui/panel.py`, wired into the header):

- **The mechanism is the Model Switcher's, call for call** (compared against
  `/mnt/PROJECTS/model-switcher/model-switcher.py`, read 2026-10-18): every call is
  `sudo -n systemctl …` with a 30 s timeout, stdout only, failures collapsing to `unknown`;
  `unit_state()` reads the first line of `is-active`; an exclusive start first **stops every
  other unit that is active or activating** — all four units the switcher manages, including
  `sd-image`, so an image generator can't hold the card while a chat model starts — then
  `reset-failed` on the target, then `start`; stop is `stop` → `reset-failed`. Nothing else in
  the app runs sudo.
- **Panel UI:** a *Models* button in the header (checked state while open) reveals a card with
  one row per chat model: status light + status word + Start/Stop, with the switcher's exact
  enable rules — Start off while running, waking or busy; Stop available while running, waking
  or failed; both on when failed; off otherwise. Names, taglines, descriptions and sizes are
  the switcher's copy. The footer carries the policy line: *"Nothing starts by itself — a model
  runs only when you start it."*
- **Threading + polling:** operations run on a worker thread (`Switcher`), all controls freeze
  while one runs, and the footer shows the steps ("Stopping Qwen3-8B…" → "Starting
  Qwen2.5-Coder-14B…"). The `is-active` poller ticks every 2 s **only while the panel is open**
  (the standalone switcher polls always; a hidden panel doesn't need sudo subprocesses). After
  a successful start the picker auto-selects that model, so it's ready to chat the moment it
  loads.
- **Never auto-start:** the only caller of `start_model` is the user's click. No boot path, no
  reconnect path, nothing else. (Units are disabled + `Restart=no` anyway.)

**Acceptance evidence (this session, 2026-10-18) — real services:**
`tests/switcher_check.py --yes` ran the full cycle through the app's own code: initial
`Qwen3-8B: active`; exclusive start of Qwen2.5-Coder-14B (progress showed "Stopping Qwen3-8B…"
first; after: 8B inactive, 14B active; `/health` served after 38 s); stop (both inactive);
restore Qwen3-8B (`/health` after 14 s) — **final states identical to initial**. The offscreen
smoke test now also covers the panel: open/close, state-driven button enabling
(active/inactive/failed), and the all-locked busy state. `./run.sh` launched on X11 with no Qt
or stylesheet warnings in the log.

**Decisions:** the panel is a toggleable card inside the window, not a separate dialog — keeps
the Portal feel and makes "start → it joins the chat" visible on one screen. `sd-image` stays
out of the panel rows (chat models only) but *is* in the exclusivity stop list (mechanism
fidelity). Auto-selecting a just-started model is selection, not starting — allowed by the
never-auto-start rule.

**New v2 candidates from Phase 2:** "Stop everything (free the graphics card)" button (the
switcher has it); GPU readout in the panel (VRAM used + busy %, the switcher's v3 feature);
remember the panel's open state across restarts.

## 2026-10-18 — Phase 3: voice output (Kokoro) + Settings

**The model (Brandon's decision, this session).** The "larger official Kokoro" researched at the
end of Phase 1 was `csukuangfj/kokoro-multi-lang-v1_0` (54 voices; full build: model.onnx
325,560,556 B ≈ 310 MiB, voices.bin 28,200,960 B; int8 sibling ≈ 109 MiB). Brandon said size is
not a concern on his network: download the **full** build (not int8) into **this project's own
`models/` folder** and make it the app's TTS model — do not touch the existing v0_19 install
(other apps use it). Done 2026-10-18: the raw HF repo (378 files, **401 MB on disk**) fetched to
`models/kokoro-multi-lang-v1_0/` (git-ignored; `models/` added to `.gitignore`); verified
file sizes + sha-free recheck via the model's own metadata. The v0_19 install is untouched and
still usable by pointing `voice.model_dir` at it — no code changes to swap models.

**Built:**

- `app/jarvis/audio.py` — sink discovery via `pactl`, the auto-pick rule, `paplay --device=…`
  playback, the Settings chime generator.
- `app/jarvis/tts.py` v2 — the data-driven voice catalog, `KokoroEngine` (lazy load, 6 threads,
  dict_dir/lexicons auto-detected), `Speaker` (streaming sentence queue, overlapped
  synthesis/playback, instant cancel), markdown-for-speech cleaning, WAV writer.
- `app/jarvis/config.py` — `appdata/config.json`: assistant name/greeting, voice (enabled,
  **voice by name**, speed, sink, model_dir, preview_text), theme accents. Auto-created,
  healed on load, atomic writes; Settings edits are persisted immediately.
- `app/jarvis/ui/settings.py` — the Settings dialog: spoken-replies toggle, voice picker
  (built from the installed model's catalog), Preview, speed slider 0.5×–2.0×, output-device
  picker ("Automatic — …" first, then every sink including the virtual ones), Test sound.
  Modeless, apply-as-you-go (the Portal way — no OK to forget).
- Wiring in `window.py`: streaming deltas collect complete sentences; synthesis of the next
  sentence overlaps playback of the current one (no gaps); a new question or toggling voice off
  cancels instantly; closing cancels; Preview speaks even while spoken replies are off.
- Branding/theme from config: window title, header name, idle greeting + “Hey <name>” caption,
  system prompt name, accent/background via `theme.apply_overrides()` — all retunable in
  `appdata/config.json` without code changes (rebranding groundwork). The voice settings moved
  out of `state.json`; state keeps model/messages/window only.

**Voice handling rules (Brandon's call):**

- Stored and chosen **by voice name, never by sid**; the sid is resolved against the installed
  model at synthesis time. bm_george is default by name (sid **9** in the old 11-voice build,
  sid **26** in the multi-lang build — both read from model metadata, never hardcoded).
- Catalog resolution at runtime, no code changes for model swaps: (1) `voice_names.txt`/`.json`
  in the model folder (manual override) → (2) the model's own ONNX metadata
  (`speaker_names`/`id2speaker`/`n_speakers`, read with a stdlib-only protobuf walk — fast even
  on the 325 MB file) → (3) a built-in seed for known builds → (4) generic `voice_N` labels
  sized by the model's `num_speakers` after load. Swapping in another Kokoro model = point
  `voice.model_dir`; its voices just appear in the picker.
- If the configured voice is missing from the installed model → falls back to its first voice
  (and persists that choice).
- Audio default: the **real desktop speakers**, never the Sunshine streaming sinks or the
  Portal null sink; `paplay --device` pins playback without touching the system default
  (other apps keep their routing). Virtual sinks stay selectable by hand in Settings.
  (**Corrected 2026-10-19 — see the Phase 4 section: this rule sent Jarvis's voice to a path
  Brandon does not hear; the auto-pick now follows the system default sink.**)
- Engine: lazy load on the speech worker (~2.4 s, ~350 MB) + background preload at launch when
  voice is enabled; `num_threads = max(2, cpu_count // 2)` = 6 on this box (measured sweet
  spot; RTF ≈ 0.6–0.7).

**Acceptance evidence (this session, 2026-10-18):**

- `tests/smoke.py` (offscreen; state + config isolated to a temp dir): config defaults
  (bm_george by name, model_dir → project model); **54-voice catalog from the model's own
  metadata** (unique names, first `af_alloy`, bm_george labeled “UK male”); auto-pick never a
  virtual sink; Settings wiring — pick bm_lewis → persists **by name** to config.json and
  reaches the speaker; speed 1.5× → persisted + applied; on/off toggle → persisted + applied;
  missing voice → af_alloy fallback **persisted**; catalog refresh keeps the current pick;
  chime writes; all Phase 1/2 assertions still pass.
- `tests/voice_check.py` (new): real model load **2.4 s / 54 speakers**; `bm_george → sid 26`
  resolved by name; preview line **5.45 s audio in 3.65 s (RTF 0.67)**; pitch ≈ 172 Hz (male
  register); `--play` spoke it on `alsa_output.pci-0000_00_1b.0.analog-stereo` (Built-in Audio
  Analog Stereo).
- Live acceptance through the app (offscreen window; **audible by design — a short chime plus
  two short spoken lines played on the desktop speakers**): Settings opened showing
  bm_george / 1.0× / “Automatic — Built-in Audio Analog Stereo”; Test sound played the chime;
  Preview spoke the sample line; a streamed reply (“Sure thing. Two plus two is four. Anything
  else I can help with?”) was spoken sentence-by-sentence as deltas arrived; queues drained;
  **zero leftover WAVs** afterwards. The LLM hop was not live: all buster units were inactive
  during acceptance (`:11434` not listening), and starting services is outside this job's
  containment — the chat loop itself is Phase 1/2-accepted code. To hear the full loop, start
  buster-ai in the Models panel and send any message.

**Bigger/newer model note (for the record):** sherpa-onnx docs now list
**`kokoro-multi-lang-v1_1`** (Chinese + English, **103 speakers**) — if a future phase wants it,
it drops in the same way (new folder + model_dir + metadata already carries the names).

**Decisions:** sentence-level streaming with overlapped synthesis (Jarvis starts ~one sentence
in, not after the whole reply); the queue carries voice *names* and resolves sids at synth
time; “Automatic” is a real picker entry (= `sink: null` in config), not a hidden mode; the
chime is generated, not shipped; Settings stays a modeless dialog (Phase 3 brief said “Settings
with voice picker + preview + speed”, and apply-immediately was the Portal-style call).

**New v2 candidates from Phase 3:** replies in the model's other languages with a matching
voice auto-pick (the 54-voice catalog includes Spanish/French/Hindi/Italian/Japanese/Portuguese/
Chinese); a Settings field for the preview line (config-editable today); per-app volume or a
quick mute (already on the list); int8 model if CPU ever becomes the constraint here.


## 2026-10-19 — Phase 4: push-to-talk speech input + audio routing correction + wake-word feasibility

Phase 4 brief (Brandon, this session): (1) fix the audio auto-pick — a Phase 3 acceptance miss,
(2) build push-to-talk STT with a locally downloaded model, (3) a wake-word feasibility report —
**report only, no code and no installs for it**.

### Item 1 — audio routing correction (the Phase 3 miss)

**Finding (verified live, 2026-10-19):** Brandon hears this PC **through Moonlight** — the PC's
system default sink is `sink-sunshine-stereo` (Sunshine captures it; Moonlight carries it to
him). Phase 3's auto-pick rule (“the real desktop speakers — never the Sunshine streaming
sinks”) therefore routed Jarvis's voice to the motherboard analog jack: a path he cannot hear.
He never heard the Phase 3 chime or voice until `appdata/config.json` was hand-set to
`voice.sink: "sink-sunshine-stereo"`. That was a straight miss in Phase 3's auto-pick design —
correcting the record here.

**Fix:** `audio.auto_pick()` now resolves in this order: **(1)** an explicitly configured sink
(unchanged — config always wins), **(2)** the **system default sink** (`pactl get-default-sink`),
whatever kind it is — for most people that is exactly where they hear things, **(3)** only when
there is no usable default: the old physical heuristic (analog → RUNNING → first physical).
Virtual sinks are legitimate destinations; they stay listed and labeled in the picker. The
“never the Sunshine sinks” language is gone from the code, docstrings, the Settings hint, and
the README.

**The hand-set config was not clobbered** — `appdata/config.json` is untouched by this phase
(still `{"voice": {"sink": "sink-sunshine-stereo"}}`; the new `stt` keys heal into memory and
only get written when a setting is next changed). With `voice.sink: null` (Automatic), Jarvis
now follows the system default — the Moonlight path.

### Item 2 — push-to-talk speech input (local SenseVoice)

**Model choice — SenseVoice int8 (239 MB), not Whisper.** Facts from the HF API + this box:

- `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`, **int8 239 MB** single model,
  **non-autoregressive** — measured here: bundled 7.15 s English clip → **0.61 s decode
  (RTF 0.08)**; 3.8 s Kokoro clip → 0.38 s (RTF 0.10).
- `sherpa-onnx-whisper-base.en` int8: 160 MB — faster class but visibly weaker English accuracy
  on names/commands.
- `sherpa-onnx-whisper-small.en` int8: 375 MB (encoder + decoder) — the most accurate English
  of the three, but autoregressive; on this Xeon (no GPU help) a short push-to-talk utterance
  costs seconds, and push-to-talk lives on latency.

SenseVoice decodes an entire utterance in one non-autoregressive pass — sub-second here — with
English accuracy that held up in testing (below). It is also multilingual (zh/en/ja/ko/yue), the
same family spirit as the TTS model; `stt.language` pins “en” (default) or `""` for auto-detect.
Downloaded to `models/sense-voice/` (git-ignored): `model.int8.onnx`, `tokens.txt`, LICENSE,
README, and the repo's `test_wavs/en.wav` (used by the checks). **Nothing else was installed.**

**Recorder — a measured gotcha, fixed:** the first live capture came back 2.00 s from a 4 s
hold. Diagnosis on real hardware: `parecord`'s default target latency buffers ~1 s that is
**dropped at stop** (5.00 s hold → 4.00 s WAV, twice). `--latency-msec=100` → 5.00 s → 4.90 s;
raw `parec` at 50 ms → 4.95 s. Fix: `--latency-msec=100` in the recorder (keeps the tail, ample
headroom against dropouts). Re-verified via the app's own `Recorder` (SIGTERM): 4.00 s hold →
3.90 s WAV.

**Built:**

- `app/jarvis/stt.py` — `Recorder` (single `parecord` capture, 16 kHz mono s16le WAV),
  `SttEngine` (lazy thread-safe SenseVoice load, 6 threads, `use_itn` punctuation),
  `TranscribeWorker` (one decode per request on a daemon thread; Qt signals back; deletes the
  WAV after use).
- **Config** (`appdata/config.json`, healed like everything else): `stt.model_dir` ("" = the
  project's own), `stt.source` (null = Automatic = system default source), `stt.language`.
- **UI** — press-and-hold on the big home mic **and** a compact mic beside the message box (the
  home mic leaves the screen once a conversation starts). Recording state: red pulsing
  face/ring + “Listening… release to send”; transcribing: accent breathing + “Transcribing…”.
  Release → transcript into the chat **exactly like typed words** (then the Phase 3 voice
  path). No model live (or still replying)? The words are staged in the message box with an
  honest notice — never lost, never sent into the void.
- **Honesty rules:** empty transcript → “Didn't catch that…” and nothing sent; failure → the
  real error is shown; holds under 0.35 s → “didn't catch that”, no invented text. Talking
  while Jarvis speaks cancels his speech (interruption, like a person).
- **CPU discipline:** decode always on a worker thread; the model loads lazily at the **first
  press** (background thread, overlapping the recording); 60 s watchdog auto-stops a stuck
  hold; window close drops any in-flight recording. The app still never auto-starts llama.cpp
  units.
- **Settings:** new **Voice input** card — mic picker (“Automatic — <default source>” first,
  then real sources; `.monitor` sources are excluded — they capture output, not a microphone),
  persisted immediately by device name.

**Evidence:**

- `tests/smoke.py` extended (offscreen; now also points `state.PROJECT_ROOT` at the temp dir so
  even the recorder's appdata folder is isolated): stt defaults + healing of bad shapes; the
  auto-pick follows the system default sink; the installed SenseVoice model is detected;
  `parecord` argv per device (Automatic = no `--device` pin); press → both mics in recording
  state + caption; release → WAV handed to the worker, both mics show transcribing; transcript
  with no model → staged in the composer + notice; with a live model → `send_message`, exactly
  like typed; empty → “Didn't catch that”, nothing sent; failure → shown; short hold → not
  transcribed; mic picker excludes monitors, persists the Fifine by name, clears to Automatic.
  Recording and the worker are stubbed on purpose (no mic, no 240 MB load offscreen); nothing
  audible or microphone-shaped is faked. All Phase 1–3 assertions still pass.
- `tests/stt_check.py` (new): bundled English clip → “The tribal chieftain called for the boy
  and presented him with 50 pieces of code.” — **7.15 s in 0.61 s, RTF 0.08** (that clip's
  reference line ends “…fifty pieces of gold”; one word off). Kokoro → SenseVoice round-trip:
  George speaks a 12-word line (3.8 s), SenseVoice returns **the same sentence, 100 % of the
  reference words** (RTF 0.10) — text → voice → text proven without a microphone.
- **Real microphone, this machine:** captured the Fifine through the app's own `Recorder` and
  decoded for real. Honest details: three short captures (4–5 s) of the room, which was quiet
  (probe floor ≈ **−59 dBFS**); transcripts were single ambient words (“The.”; “I.” once the
  recording window was fixed) — **no one spoke a test phrase into the mic during this
  session**, so the capture + decode path is what was exercised here; the speak-a-phrase test
  is Brandon's acceptance below.
- Worker-thread path with the real model + Qt event loop (offscreen probe): load and decode off
  the UI thread, queued signal delivered, WAV cleaned up after use.
- `tests/voice_check.py` re-run: RTF 0.66 / 172 Hz — the Phase 3 path is unchanged.

**Remains for Brandon's acceptance:** (1) `./run.sh`, hold the mic on the home screen (or the
one beside the message box), speak, release — the transcript should land in the chat and
George's spoken answer should come back **through the Moonlight path**. (2) Settings →
**Test sound** still lands on the configured sink after a restart (`voice.sink` untouched in
`appdata/config.json`). (3) The Settings **Voice input** picker reads “Automatic — …” with the
Fifine.

### Item 3 — wake-word feasibility (“Hey Jarvis”), report only

No code, no installations — findings only, for a separate decision.

**What always-listening means in practice:** the microphone stays effectively hot (on PipeWire
this box's Fifine source stays `RUNNING`, suspending only when idle). Audio never needs to leave
the machine for any candidate below — but the privacy shape changes from “the mic is off until
I hold a button” to “something is always listening”. Push-to-talk (just built) remains the
reliable path; the wake word would only replace **reaching for the button**, not the rest of
the pipeline (record → SenseVoice → chat → Kokoro). Expected UX: wake → short chime → record
until silence → transcribe → the same Phase 4 path. Half-duplex matters: Jarvis's own voice can
reach the mic, so a wake listener should pause while he speaks (the Phase 4 mic already stops
his speech when you talk).

**Candidates, honestly assessed (none was installed or benchmarked live):**

1. **openWakeWord** — Apache-2.0, Python (`openwakeword`), on-device. Ships a pretrained
   **`hey_jarvis`** model — exactly the phrase asked for. Tiny frame-based CNNs (a few MB; mel +
   embedding + wake model) at 16 kHz / 80 ms frames; it targets Raspberry-Pi-class hardware, so
   on this Xeon the expected cost is a couple percent of one core at most. False wakes are
   tunable via a confidence threshold; some triggers from TV/background speech are possible at
   low thresholds — a VAD gate and threshold tuning mitigate. Needs `onnxruntime` (or
   tflite-runtime) as a new dependency — an install, hence ask-first. **Best fit overall.**
2. **sherpa-onnx keyword spotting** — already in the stack (no new engine). Apache-2.0, CPU
   cost similarly tiny, but the released pretrained KWS models are Chinese
   (WenetSpeech-class); a turnkey English “hey jarvis” model is not published. Not turnkey.
3. **Porcupine (Picovoice)** — “Jarvis” is literally one of its built-in keywords; very low CPU;
   mature false-wake tuning. But it is proprietary/closed-source with a registration-gated free
   tier (commercial use is paid — verify terms at build time); key validation touches the
   vendor at setup. Versus openWakeWord's identical phrase support under Apache-2.0, there is
   little reason to take the licensing friction.
4. **VAD-gated SenseVoice loop (zero new engines)** — use the stack already installed: a
   sherpa-onnx VAD (silero, ~2 MB) runs continuously; when it hears speech, send the segment
   through the already-installed SenseVoice and match “hey jarvis” in the text. Idle cost ≈ VAD
   only (negligible here); anything spoken is transcribed for real. False wakes: text matching
   is precise, but background speech (TV/music) gets transcribed — CPU scales with how much
   speech is around. A decent fallback if we want zero new dependencies.
5. **Vosk KWS** — Apache-2.0; this is essentially what **upstream `portal-wake` used on the
   Portal itself** (Phase 0 notes §3). A small English model (~40 MB) with keyword spotting;
   heavier and dated next to the frame-based engines, but it is the approach the original
   product shipped.

**Hardware fit (Fifine + this room):** the Fifine is a USB mic resampled by PipeWire to the
16 kHz these engines want; a quiet room helps a lot — measured ambient floor during this session
was ≈ −59 dBFS, a good SNR for wake word (the typical failure mode is steady fan/PC noise near
the mic — worth a placement check by ear). False wakes can't be quantified without building;
expect “rare, tunable” at sane thresholds.

**Recommendation:** **worth building later, as an opt-in phase** — not now. Concrete shape:
openWakeWord `hey_jarvis` (Apache-2.0) + a VAD/verification gate, reusing the entire Phase 4
pipeline, behind a Settings toggle with an unmistakable on-screen listening indicator (and the
wake listener muted while Jarvis speaks). Expected CPU cost on this box: negligible. The real
prices: an always-hot mic, one small dependency, and some false-wake tuning. Push-to-talk
already covers the need today; if Brandon wants hands-free later, the zero-install variant
(option 4) can prove the idea with a VAD alone before adding openWakeWord.

**Decisions:**

- Item 1 correction: auto-pick follows the **system default sink**; virtual sinks are
  first-class choices (his Moonlight path is one). The saved config was left alone.
- Item 2: **SenseVoice int8** (latency over absolute accuracy); recorder pinned to
  `--latency-msec=100`; the transcript enters the conversation as a normal user message; the
  mic picker lives in Settings with Automatic = system default source; STT model, device and
  language all in `appdata/config.json`.
- Item 3: report only — openWakeWord is the recommended engine if/when it is built; no installs.

**New v2 candidates (Phase 4):** wake word as its own opt-in phase (openWakeWord hey_jarvis +
VAD gate; see above); a global push-to-talk hotkey (hold `Ctrl+Space` even when the app is not
focused); an STT language picker in Settings (multilingual SenseVoice is already installed, so
non-English dictation is mostly a config/UI matter); a keep-mic-warm option if first-press wake
latency on the suspended USB mic ever bothers (the buffered-tail loss is fixed at 100 ms;
device wake itself is PipeWire policy).

---

## 2026-10-19 — Phase 5: “hey jarvis” wake word (opt-in)

Built exactly as pre-approved: **openWakeWord** as the always-listening engine, its model files
in this project, off by default, with an unmistakable-but-truthful indicator. Push-to-talk
remains the reliable path; nothing else in the app changed shape.

### What was installed, and from where

- `openwakeword 0.4.0` + `onnxruntime 1.31.0` (which came along as its dependency) via
  `python3 -m pip install --user --break-system-packages openwakeword` — the user site
  (`~/.local/lib/python3.12/site-packages`), where `sherpa_onnx` already lived. PEP 668 blocks
  plain `--user` on this box, so `--break-system-packages` is what actually installs. **0.5.1
  and 0.6.0 are not installable on Python 3.12** — both demand `tflite-runtime`, which has no
  3.12 wheel (dry-run checked); 0.4.0 is the newest usable release and works with ONNX.
- 0.4.0 ships its models inside the wheel (no `utils.download_models`). The three files the
  listener needs were copied into the project's own `models/openwakeword/` (git-ignored, like
  the other models) and **loaded from there via explicit local paths** —
  `Model(wakeword_model_paths=[…hey_jarvis…], melspec_onnx_model_path=…, embedding_onnx_model_path=…)`,
  which 0.4.0 forwards to `AudioFeatures(**kwargs)` (source-verified). site-packages is only the
  code, never the runtime model source:

  | file | bytes | sha256 |
  |---|---|---|
  | `melspectrogram.onnx` | 1 087 958 | `ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f` |
  | `embedding_model.onnx` | 1 328 103 | `ba754db3cd768a524c655ea90655ee5e6055a43b8dfd29366a11e93716ae9e51` |
  | `hey_jarvis_v0.1.onnx` | 1 271 370 | `94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb` |

  (One cosmetic wart: `AudioFeatures` asks onnxruntime for a CUDA provider unconditionally and
  ORT 1.31 warns about it once per session; the listener filters that one known warning at
  build time — CPU is used either way. License: Apache-2.0.)

### What was built

- **`app/jarvis/wake.py`** (new) — `WakeListener`, a `QObject` with its worker thread: raw
  `parec` stream of the resolved source → 1280-sample (80 ms) frames → `Model.predict` →
  Qt signals `ready` / `wake(score)` / `failed(reason)`. Two consecutive frames ≥ threshold
  fire one wake; the listener then **pauses itself** until the app resumes it (one phrase,
  one fire), with `model.reset()` on resume so the suppressed audio can't leak into the next
  detection. `pause()` still drains the pipe (stream stays real-time) but scores nothing.
  `stop()` frees both stream and model — off means off. Wake log helpers:
  `appdata/wake_log.jsonl`, one JSON line per wake `{ts, score, source}`.
- **`app/jarvis/stt.py`** — the recorder was unified onto one capture path: `parec` streaming
  raw s16le 16 kHz mono, reader thread writes the WAV itself (Phase 4's `parecord --file-format=wav`
  couldn't hand us audio until the file closed, and the wake flow needs it *as it arrives*).
  New `AutoStop` mode = energy VAD: burns in a room-noise floor (20th percentile) during the
  first 0.35 s, speech must clear floor+10 dB clamped to −55…−35 dBFS, then the take ends after
  1.0 s of trailing silence, or after 6 s if nothing was ever heard, or at the 15 s hard cap.
  `last_reason` / `last_heard_speech` report honestly how it ended. Push-to-talk is the same
  call with no auto-stop — API unchanged from the outside.
- **`app/jarvis/ui/window.py`** — one reconciler (`_sync_wake`) decides the listener's real
  state from config + what's happening: off / starting / listening / paused / error.
  Suppressed while Jarvis speaks, while any recording runs, while transcribing — plus 2.5 s
  after speech ends (he must not wake himself) and 1.5 s after a wake (cool-down). Wake flow:
  log → chime (the Settings chime, through the configured sink) → ~0.65 s → recording with
  `AutoStop` → transcript into the same send path as push-to-talk. Esc **or** the mic button
  cancels a wake take and discards it. A missing configured source stops the listener with a
  named reason (never a silent switch) and retries every 5 s. Header badge: hidden when off;
  `● Listening for “hey jarvis”` in accent while truly listening; dim “paused” / “starting”;
  red “unavailable” with the reason in its tooltip.
- **`app/jarvis/ui/settings.py`** — “Wake word” card: the off-by-default toggle, a trigger
  slider 0.30–0.90 (step 0.05, default 0.50), and the logged-wake count (read from the log
  honestly). **`app/jarvis/config.py`** — `wake: {enabled, threshold, model_dir}` with the same
  healing discipline as everything else (bad shapes → off / 0.5 / project default; out-of-range
  clamps).

### Verified — commands and numbers

- **Offscreen smoke** (`QT_QPA_PLATFORM=offscreen python3 tests/smoke.py`) — extended, green.
  Covers: wake config defaults + healing (`enabled:"yes"`→off, `threshold:12`→0.9, `"abc"`→0.5);
  the Settings toggle/slider persisting and reaching the listener; the badge appearing when
  enabled and vanishing when disabled; the wake flow (stubbed chime, shortened delays) through
  chime → recording → auto-stop → transcription → the same send path; the log line with its
  score; suppression while a take is in flight, while Jarvis speaks, and through the quiet
  tail; push-to-talk suppressing detection; Esc discarding a cancelled take; the missing-mic
  error path stopping (not switching) and restarting after retry; the Settings count reading
  the log. **The listener is stubbed in this suite and emits no wake results — stubbed is
  stubbed; detection was checked separately, below.**
- **Wake-word check** (`python3 tests/wake_check.py`, offline; Kokoro speaking, since no human
  is at the mic during checks):
  - load from the project's files: **1.2 s**; silence: max 0.0001; negative control (a speech
    clip, no wake phrase): max 0.0036 — clean.
  - “Hey jarvis.” **fires on all three synthetic voices**: bm_george 0.9973, am_michael 0.8537,
    af_alloy 0.9968 (threshold 0.5). This is a *synthetic-voice* result — the real-voice test
    is Brandon saying it.
  - the same script drives the real `AutoStop` tracker: speech clip (1.20 s) + 2.5 s of digital
    silence → stops at **2.00 s**, reason `silence`, speech heard true; digital silence alone →
    stops at **6.00 s**, reason `timeout`, heard false.
- **Live listener, real mic** (`python3 tests/wake_check.py --live 75 --warmup 15`, on
  `laptop_mic`, the configured source): 921 frames scored = 73.7 s of audio processed in
  real-time, **0 wakes**, max score 0.0113 (a second 60 s window: 0 wakes, max 0.0423).
  **Idle CPU: 6.41 s of CPU over the 60 s steady window = 10.7 % of one core** (11.8 %
  including load; ≈ 1.8 % of this 6-core box). **Memory: 43 MB python baseline → 154 MB with
  the listener loaded, peak 220 MB (+178 MB peak).** A separate throwaway bench explains the
  CPU: at the canonical 80 ms frames the whole pipeline costs 64 ms per audio-second
  (RTF 0.064), of which **5.2 ms/frame is openWakeWord 0.4.0's own mel preprocessor** (it
  rebuilds from its growing raw buffer each frame) and only 0.18 ms/frame the three ONNX
  networks. Feeding bigger chunks (160/240/320 ms) would cut CPU to 56/52/47 ms per
  audio-second, but 80 ms is the model's native frame — kept it, and this number is reported
  instead of the Phase 4 report's optimistic “a couple percent of one core”.
- **Recorder regression** (`python3 tests/stt_check.py --mic 4`): 4.00 s capture from the
  bridged laptop mic → transcribed in 0.40 s (RTF 0.10). It even caught room speech live
  (“Open.” — real audio over the bridge, not staged).
- **AutoStop on the real mic** (`python3 tests/stt_check.py --autostop --mic 12`): the room had
  continuous speech during the check (TV/stream dialogue through the bridge); the VAD heard it
  (`heard true`) and the take rode to **the 12 s cap** — exactly what the cap is for. In a room
  with continuous loud speech the 1 s silence stop rarely gets a gap; the 15 s cap bounds it.
  (Transcribing that take returned garbled TV dialogue — real audio, honestly reported.)
- **Voice path untouched**: `tests/voice_check.py` still green (am_michael resolved by name,
  RTF 0.79, pitch ~151 Hz).
- **Real config, real app**: `QT_QPA_PLATFORM=offscreen python3 -m jarvis` stayed alive with
  Brandon's own `appdata/config.json` (wake off → no listener); the config file's sha256 is
  **byte-identical before and after** — healing happens in memory, nothing is rewritten until
  the user changes something.

### Decisions and honest limits

- **Off means off / truth in the badge.** The listener is only started while the toggle is on;
  the badge says “listening” only when a model is loaded *and* the stream is open *and* the
  badge is not shown in any other state. Stopping frees the model (no background cost).
- **Detection quality on real voice and real TV is not yet proven here** — the automated
  results are synthetic-voice and quiet-room. Live windows showed 0 false wakes with maxima
  ≈ 0.01–0.04, but no TV was playing during them; that acceptance run is Brandon's. If false
  wakes ever appear, the trigger slider is the knob (and every wake is timestamped in
  `appdata/wake_log.jsonl` so the evidence can be counted, not guessed).
- Suppression windows (2.5 s after speech, 1.5 s after a wake) are deliberately on top of the
  paused states; they cost at most a couple of seconds of listening latency after George stops
  talking — the price of not having George wake himself through the room.
- The auto-stop threshold is adaptive but simple (energy only, no VAD model as pre-approved);
  very steady loud noise can keep `heard` false in a loud room — the 6 s no-speech timeout ends
  the take and the app says “Didn't catch that” instead of inventing a transcript.

### Brandon's acceptance test (the one that counts)

1. `./run.sh`; start a model in the Models panel if none is live; Settings → check “Listen for
   hey jarvis”; the badge “● Listening for hey jarvis” appears in the header.
2. From the bridged laptop mic, say “hey jarvis” + a question: chime → the mic states show
   listening/transcribing → transcript in the chat → George answers through the Moonlight path.
3. Uncheck the toggle: badge gone, mic cold (listener stopped, model freed).
4. With the TV playing, watch `appdata/wake_log.jsonl` for a while — false wakes should stay
   rare or absent at 0.50; raise the trigger if the TV sneaks one in.

---

## 2026-10-19 — Phase 5.1: the soak findings fixed (room-relative takes; wake verification)

Brandon's soak of Phase 5 found two defects; both are fixed here. Nothing else changed shape —
after an **accepted** candidate the flow is exactly Phase 5's: chime → 650 ms → auto-stopping
take → the same local transcription → the same send path.

### The findings

1. **Takes never ended in his room.** “Room silence” through the bridged laptop mic measures
   ≈ −19 dBFS (TV + open speakers), but the Phase 5 tracker clamped its silence threshold to
   −55…−35 dBFS — *below* the bed — so once `heard` was true nothing ever looked like silence
   and takes rode to the 10–15 s cap.
2. **The TV fired the wake word.** *Marshals* dialogue scored 0.88 / 0.939 / 0.965 — the same
   range as his own voice. No trigger slider can separate those; separability had to come from
   a second signal: the actual words.

### Issue 1 — end-of-speech is relative to the room, never absolute

- `stt.NoiseFloor`: trailing ~10 s window of 80 ms frame levels, 10th percentile, valid after
  5 frames; `stt.frame_level_db()` is the single RMS helper (also used by the listener).
- `stt.AutoStop` now carries `margin_db` (default 8), `silence_seconds` (default 1.0),
  `max_seconds` (15 — backstop only), `no_speech_seconds` (6 — unchanged). The old
  `offset_db` / `min_threshold_db` / `max_threshold_db` / calibration fields are gone.
- `_SilenceTracker` seeds its floor from the wake listener's live estimate
  (`Recorder.start(floor_db=…)`) and **stops learning the floor the moment speech is heard**,
  so a voice cannot drag the line up with it; the take ends after `silence_seconds` back at
  the floor.
- `WakeListener` feeds every scored frame into its own `NoiseFloor` (cleared on resume — the
  room after TTS isn't necessarily the room before it) and exposes `noise_floor_db`; takes
  also record `last_floor_db` / `last_threshold_db` for diagnostics.
- Settings: **Hang time** 0.5–3.0 s (default 1.00) and **Margin** +4–16 dB (default +8);
  both persist immediately and heal in `config.load()` like every other key
  (`wake.silence_seconds` / `wake.speech_margin_db`).

**Measured, synthetic** (`tests/wake_check.py`): steady bed at −19.0 dBFS measured, Kokoro
speech mixed +10.2 dB over it, speech ends at 1.60 s:

| tracker on the same mix | result |
|---|---|
| Phase 5 math (clamped threshold) | **did-not-stop** (heard true immediately; rides to the cap) |
| Phase 5.1, unseeded | **silence at 2.32 s = speech end + 0.72 s**, floor −19.2, line −11.2 |
| Phase 5.1, listener-seeded (−19) | identical |

Quiet room stays green: 1.20 s clip + 2.5 s silence → stop 1.84 s, reason `silence`; digital
silence alone → 6.00 s, reason `timeout`, heard false.

**Measured, real audio** — 8 s capture from `laptop_mic` with the Moonlight stream playing:
overall −20.1 dBFS; frame levels −24.6 / −20.5 / −16.5 (min/median/max). On that same capture:
old math **did-not-stop** (the bug, reproduced on the real room); new math floor −23.0 → line
−15.0, ends by the 6 s nothing-heard guard (nobody spoke) instead of recording to the cap.
`python3 tests/stt_check.py --autostop --mic 12` on the live mic now ends “timeout · heard
False — nothing above the room noise floor” (Phase 5 rode to the 12 s cap in the same room).

### Issue 2 — a wake is a candidate until the transcript says the name

- The listener keeps a rolling 3.5 s frame buffer; on a trigger it reads 3 post-roll frames
  (0.24 s), writes `appdata/stt/wake_candidate.wav` (overwritten per candidate — it is the
  verification input, not a keepsake) and emits `wake(score, wav)`.
- The window runs the check on its own thread (`_start_verification` — a deliberate
  smoke-test seam) calling the existing `stt_engine.transcribe`. `SttEngine` already
  serializes load and decode (`_load_lock` / `_use_lock` from Phase 4), so the two call sites
  (PTT worker + verification) needed **no new lock** — verified, not assumed.
- `wake.transcript_has_wake()` accepts “jarvis” plus one-edit wobble (substitution,
  insertion, deletion, transposition), apostrophes stripped (“Jarvis's” → `jarviss` → one
  edit). 10/10 test cases, including the real error SenseVoice makes: “Hey, Javis.”
- The verdict lands on the UI thread: **every** candidate is appended to
  `appdata/wake_log.jsonl` as `{ts, score, source, text, accepted, reason}` — reasons `""` /
  `no-wake-word` / `no-audio` / `stt-error` / `verify-error`. `no-wake-word` is a **silent
  rejection**: no chime, no recording, no send; a short cool-down re-arms the listener.
  Accepted → Phase 5 flow unchanged. Settings counts accepted and rejected separately; old
  Phase 5 log lines (no `accepted` field) count as accepted — they were never verified, and
  pretending otherwise would be dishonest.
- While a candidate verifies, `_verifying` suppresses the listener exactly like a recording
  (badge shows paused), and push-to-talk waits its turn instead of racing the verdict.
- **Verification latency — the number before the chime:** post-roll 0.24 s + decode
  **0.20–0.23 s warm** for the 1.8–2.6 s candidate (RTF ≈ 0.08), so the chime lands
  ≈ 0.45–0.5 s after the phrase. The first verification after a fresh start also pays the
  one-time SenseVoice load (1.32–1.39 s measured) — once.

**Measured, end to end** (real listener over a synthetic parec stream, real openWakeWord +
real SenseVoice): fired 0.994 → captured a 1.8 s candidate → “Hey, Javis.” → **accepted in
0.23 s**.

### Everything else, re-measured — all suites green

- **Offscreen smoke** (extended; listener and verdicts stubbed — the suite tests wiring, the
  real engine path is measured above): verification accept, rejection with no chime/no
  recording, in-flight suppression + badge truth, floor seed and Settings values reaching the
  take, log fields, healing of the new keys, both sliders, separate counts.
- `tests/voice_check.py`: am_michael by name, RTF 0.66, pitch ~151 Hz — untouched.
- `tests/stt_check.py` bundled clip → RTF 0.08; `--mic 8` from `laptop_mic` → RTF 0.08.
- `tests/wake_check.py --live 75 --verify` on `laptop_mic` with the stream playing: 926
  frames (74.1 s), **0 candidates**, max score **0.3553** — context, not comfort: TV-like
  audio can get within 0.15 of the 0.50 trigger, which is exactly why the transcript verdict
  is the separator. A second 30 s window: max 0.0133, floor estimate **−22.6 dBFS**. CPU
  steady **10.7 % of one core** (Phase 5: 10.7 %); RSS **+178 MB peak** (Phase 5: +178 MB) —
  the buffer + floor estimate cost nothing measurable. The live check caught a real bug in
  passing: `stop()` cleared the floor estimate before the check could read it; the last
  reading is now kept (start() resets it).
- **Real config, real app**: `QT_QPA_PLATFORM=offscreen ./run.sh` for 20 s with Brandon's own
  config (wake **on**, 0.5): clean run, no candidate, `appdata/config.json` **byte-identical**
  before and after (sha256 `a2ef8f1a…` — his own post-soak file; the Phase 5 report's hash
  described the pre-soak file before he toggled the feature on), `appdata/wake_log.jsonl`
  untouched (227 bytes).

### Honest limits

- Energy VAD still cannot end a take while TV dialogue is continuously *above the line*; the
  15 s cap is the backstop and the **Margin** slider is the knob (raise it if the TV holds a
  take open; lower it if your voice isn't being heard). A steady loud room with no speech
  ends at the 6 s guard and says “Didn't catch that”.
- The fuzzy name match is one edit. If real transcripts wobble further, the rejected log
  lines carry the exact text; widening is a one-line change — with data.
- Verification adds ~0.5 s before the chime (warm) and one first-time ~1.4 s after app start.
  Chime-before-verify was rejected: rejection would then be audible, which the brief forbade.
- The smoke suite's verdicts are injected; only wake_check (offline + live) exercises the real
  engine's decode inside the verification path. The real-voice acceptance is Brandon's.

### Brandon's acceptance test (the one that counts)

1. `./run.sh` — his config already has the wake word on; the header badge should read
   listening.
2. With the TV playing (*Marshals* etc.): say “hey jarvis” + a question. The chime comes a
   beat after the phrase, the take ends about a second after he stops, and the transcript +
   reply proceed exactly as before.
3. Open `appdata/wake_log.jsonl`: his wakes should be `"accepted": true` with the text; TV
   candidates should be `"accepted": false, "reason": "no-wake-word"` with the transcript
   attached — the verdict readable, not asserted.
4. Settings → Wake word shows accepted vs rejected counts separately. If a take cuts him off,
   raise **Hang time**; if his voice isn't heard in a loud room, lower **Margin**; if the TV
   still holds takes open, raise it.

## 2026-10-19 — Qwen3-Coder-30B removed from the model panels (Muse, at Brandon's word)
- The model itself was deleted earlier today (18 GB HF cache + buster-coder.service removed; Brandon's verdict after the failed local-OpenCode run). This change removes its entries from app/jarvis/models.py (Jarvis Models panel) and from the standalone Model Switcher app, so neither panel offers a model that no longer exists.
- tests/smoke.py panel fixture updated to match (failed-state role moved from buster-coder to buster-14b); that file also carries Phase 6 work in progress, so this note rides with the Phase 6 commit rather than a separate one.
- Verification: both files compile; remaining panels list Qwen3-8B, Qwen2.5-Coder-14B (+FLUX in the Switcher). NOTE: tests/smoke.py currently trips at "the transcript is sent like typed words" — confirmed PRE-EXISTING: the pristine Phase 5.1 tree (e52441a, zero edits) fails the identical assertion under today's appdata state, so it is unrelated to this removal; it belongs to whoever finishes Phase 6 to run down.

## 2026-10-19 — Phase 6: read-only tools (weather + clock) — built by DeepSeek, completed + verified by Muse

Implemented to the approved brief: a deterministic intent router (no model
function-calling) in `app/jarvis/tools.py`; weather from Open-Meteo (geocode ->
forecast; WMO codes -> plain words; results written for the ear), the clock from
the PC's own time; results injected as a marked system block ahead of the user
message; one display-only tool note per lookup in the transcript; every call
(ok or failed) appended to `appdata/tools_log.jsonl`; failures honest (the model
is told to say the lookup failed, never invent a temperature). Settings gains a
Tools card: master toggle (default ON), default place (default "Frankfort,
Kentucky"), Fahrenheit/Celsius. With tools off, the system prompt is the
Phase 5.1 text byte-for-byte and the router never runs. New healed `tools`
config section. `tests/tools_check.py` (offline, recorded Open-Meteo pair in
`tests/data/`) covers the router table, place extraction, composition, and
failure shapes; the smoke suite covers wiring end to end with the lookup
stubbed at the `_run_tool` seam.

Build history, for the record: DeepSeek V4.1 Flash Free wrote the bulk of it
before the account-wide free-tier cap stopped it at ~15 minutes; a local
Qwen3-Coder session then overflowed its context and degenerated without
landing the finish. Brandon called the local Coder "garbage" for this work,
deleted the model, and handed the phase to Muse to complete and fix.

### The smoke failure that wasn't Phase 6's fault
`tests/smoke.py` was failing at "the transcript is sent like typed words"
(pre-existing: the pristine Phase 5.1 tree fails it too in a models-off
environment). Traced with a class-level probe: the window's real
HealthPoller starts in `__init__` and its first cycle — queued behind the
suite's heavy model loads — could land minutes into the run and overwrite
the suite's hand-stubbed model states with this machine's real ones
(all-off), flipping `_can_send()` to false so the wake transcript staged
instead of sending. The app behavior is correct; the suite wasn't hermetic.
Fix (tests only): the suite now disconnects + stops the poller after
construction and drives `_on_health` by hand; and the wake section turns
tools off for its one send (a weather-shaped transcript would legitimately
route into the async tool path and race the sync assert — the tool path has
its own section). Suite is green back-to-back, and `appdata/config.json` is
byte-identical after the runs.

### Verification (2026-10-19, Muse)
- `tests/smoke.py`: ok three runs in a row.
- `tests/tools_check.py`: ok (offline).
- Live through the app code: `weather_result("Frankfort, Kentucky")` ->
  "It's 60 degrees and clear in Frankfort. High 71, low 37, no rain
  expected." (matches the morning's Open-Meteo read); clock answers in
  natural words; `weather_result("")` uses the configured default place;
  "what is the temperature in Frankfort Kentucky" (no comma) extracts
  "Frankfort Kentucky" and resolves via the state hint.
- switcher_check NOT re-run: it prompts interactively and cycles the real
  services; Phase 6 does not touch the switcher. (The Coder panel removal
  earlier today was verified by compile + list inspection instead.)

### Brandon's acceptance test (the one that counts)
1. `./run.sh`, start the 8B from the Models panel.
2. Type or say: "What's the weather in Frankfort?" — a small note line
   ("Weather checked for Frankfort, Kentucky — …") appears, and George
   answers with the real conditions in his own voice.
3. "What time is it?" — the real time, from the PC clock.
4. Try a place: "What's the temperature in Lexington?" — the note names
   Lexington; a nameless weather question uses the Settings default place.
5. Ordinary chat ("tell me a joke") fires no tool and behaves as before.
6. `appdata/tools_log.jsonl` shows one line per lookup, failures included.

### Addendum (same day) — the Frankfurt bug, caught by Brandon's acceptance test
Brandon asked by wake word: "What's the weather in Frankfurt, Kentucky?" — the
note read "Frankfurt, Bavaria" and George relayed German numbers as Kentucky's.
Root cause, verified against the live geocoding API: the speech spelling
"Frankfurt" (with a T) searches to an all-German result list, and the old
`_geocode` returned the best of the FIRST non-empty attempt without requiring
the state in the query to match any candidate — so the "Kentucky" in his
question was silently ignored. Fix in `tools.py`: a state/country named in the
query is now a promise — only candidates actually in it are acceptable; a
-furt -> -fort spelling-variant search covers the STT spelling; a hinted
search with no in-state match fails honestly instead of wandering continents.
Bare "Frankfurt" (no state named) still resolves to Germany, correctly.
Regression coverage: new `check_geocode_discipline` in tests/tools_check.py
(offline fixtures). Live-verified after the fix: "Frankfurt, Kentucky",
"Frankfurt Kentucky", "Frankfurt, Indiana", "Frankfort Indiana" all resolve
to the right Frankforts; Lexington/Louisville/New York/Salt Lake City fine.
(Footnote: my first honest-failure test case, "Frankfurt, Ohio", turned out
to be a real place — Frankfort, Ross County — and the code resolved it
correctly; the test case was wrong, not the code. It's "Narnia" now.)

## 2026-10-19 — Phase 7: always there (tray + autostart) — Muse, at Brandon's ask
- Close-to-tray: the X now hides Jarvis to the system tray instead of quitting; everything (health poller, wake listener) keeps running, which is the point of 24/7. A one-time tray balloon explains where he went. Clicking the tray icon toggles the window; the tray menu has Open + Quit — Quit is the only in-app exit (OS/session teardown aside). Desktops with no tray fall back to the old close-quits behavior. Smoke covers the decision logic with a stub tray (absorbed close, single tip, real-quit teardown).
- Autostart: ~/.config/autostart/jarvis-desktop.desktop runs run.sh at Cinnamon login, alongside his existing front-camera/portal-display entries. Remove that file to stop him auto-starting.
- Menu entry: ~/.local/share/applications/jarvis-desktop.desktop (validated) with jarvis.png rendered from the app's own orb; the autostart entry uses the same icon. Brandon can pin it to the panel from the menu.
- Settings dialog scrolls (same-day fix, Brandon): the five cards outgrew short screens once the Tools card landed; cards now live in a scroll area, the footer/Done stays pinned below it, and the dialog caps at 92% of the screen height.
- Tray Quit fix (same day, Brandon): Quit Jarvis from the tray did nothing while hidden - close() is a no-op on a hidden window. _quit_from_tray now surfaces the window, runs the closeEvent teardown, and calls QApplication.quit() as a backstop. Smoke asserts the quit is invoked from the tray state.

## 2026-10-19 — Jarvis voice on the Portals (Snapcast) — Muse + Brandon
- Goal: only Jarvis's spoken answers play through the Meta Portals (not all PC audio). Route: voice.sink = pc_stream (the pre-existing null sink from pc-audio-sink.service) → feeder → Music Assistant's snapserver stream "PC Audio" → badaix Snapcast clients on both Portals (groups moved to PC Audio; .31 client vol 60, .32 100 + device volume max).
- TWO stacked faults found by measurement: (1) MA's snapserver TCP source (tcp://0.0.0.0:4953, the original feeder design) is inert in this build — never consumed, queue backlogged — replaced with a pipe source: /data/jarfifo in ~/docker/music-assistant-server/data (container /etc/snapserver.conf edited, backup .bak-2026-10-19). (2) THE root cause: parec (Pulse layer) captures PURE SILENCE from this PipeWire null sink's monitor (RMS -inf) while pw-cat (native PipeWire) captures the real audio (-57 dB) — which is also why snapserver called the stream "idle" all day. Feeder rebuilt: pw-cat → ~/.local/bin/fifo_feed.py (primes ~1.5 s, writes continuously) → jarfifo, in pc-audio-pipe.service (backup .bak-tcp-2026-10-19). Stream now locks "playing" with live audio.
- Client caveats (ear-verified): the badaix app does NOT auto-connect at boot — opening it connects it (audible proofs 16:35 + 16:50); never force-stop it (its service dies with the activity and wedges); a Portal reboot resets it cleanly but drops wireless ADB until Brandon re-enables it (.32 pending his reconnect). Delay: snapserver buffer is 1000 ms (MA's flag) + feeder prime — expect ~2-3 s; unmeasured by ear yet.
- Flip back to laptop audio: voice.sink = sink-sunshine-stereo (config backup appdata/config.json.bak-pre-snapcast).

## 2026-10-19 (evening) — Snapcast build COMPLETE, root cause corrected
- EAR-VERIFIED 18:13: Brandon asked Jarvis by voice; the answer came out of the living-room Portal. Final feeder: parec (NOT pw-cat) → fifo_feed.py → jarfifo.
- CORRECTION to the earlier entry: parec was never broken. The master fault was the pc_stream sink MUTED at the PipeWire node level (pw-dump mute: True, invisible in every volume view; fixed with pactl set-sink-mute pc_stream 0; post-unmute parec measures a clean -6 dB RMS). pw-cat's sink/monitor capture on this rig returns ~-55 dB residue regardless of target — abandoned. ffmpeg's sine lavfi source is only ~-21 dB peak here; use aevalsrc for test tones.
- Guard for the future: if the Portal feed ever goes silent again, check pc_stream's node mute FIRST (pw-dump | grep -A2 pc_stream), then the feeder unit, then the client.

## 2026-10-19 (evening) — Settings dialog width fix
- Brandon: the Settings box was not wide enough — options like Test sound were cut off. Root cause: the device combos (voice / mic source / output sink) used the default sizeAdjustPolicy (AdjustToContents), so a long entry (e.g. the laptop-mic tunnel name, sink descriptions) forced its row wider than the dialog, and with horizontal scrolling off the right end (Test sound) was simply invisible.
- Fix (app/jarvis/ui/settings.py): all three combos now use AdjustToMinimumContentsLengthWithIcon with minimumContentsLength 28 (they elide long names; the dropdown still shows full text), and the dialog widened (minimum 620 -> 720, initial width 660 -> 780). Verified by screenshot: every card fits, Preview and Test sound fully visible.

## 2026-10-19 (evening) — Wake chime moved to the default output
- Brandon: with the voice sink on the Portals, the wake acknowledgment chime arrived ~2-3 s late (Snapcast buffer) — after the recording window had already closed, so the "speak now" cue was worse than useless. Fix (app/jarvis/ui/window.py): _play_wake_chime now plays on the SYSTEM DEFAULT output (Sunshine/Moonlight to the laptop — instant) instead of the configured voice sink. The Settings "Test sound" button still tests the configured device, and voice replies still go to the voice sink (Portals) untouched. Smoke test passes.

## 2026-10-19 (evening) — Wake chime sound replaced
- Brandon rejected the stock two-sine chime ("terrible", "like 1993 text message alerts") and two audition batches before picking candidate #5 "Groovy Orbit" from a batch of 10 originals (AI/hippy/party/helpful brief): kalimba A4-A4-D5 with a soft F#5 digital blip. appdata/chime.wav replaced (generator scripts were session /tmp/make_chimes10.py; the wav survives in appdata; original saved as appdata/chime.wav.orig-2026-10-19). No restart needed — play_file reads the file per wake.

## 2026-10-19 (evening) — Jarvis learns to say Frankfort
- Brandon: Kokoro said "Frank-Fort" (hard T); the town is said "Frank-furt". Fix (app/jarvis/tts.py): a spoken-respelling table in clean_for_speech maps Frankfort -> Frankfurt for SPEECH ONLY. Display text keeps the true spelling and the weather geocode is untouched (the Phase 6 geocode fix needs "Frankfort" — spelled the German way it resolves to Bavaria). Smoke test passes; verified clean_for_speech output directly.

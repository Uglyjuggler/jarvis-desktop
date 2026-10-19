"""Jarvis's system prompt — the desktop equivalent of the upstream SessionPrompt.

Mirrors rudysev/portal-assistant `conversation/SystemPrompt.kt` (role line) and the
spoken-style guidance the upstream host adds (`host-assistant` HOST_SYSTEM_SUFFIX:
"Keep every spoken reply to at most three short sentences"), plus the device
time/date context that upstream `SystemContext` injects.

Phase 6: when the read-only local tools are enabled, the prompt tells the model
that a "Tool result" block (when present) is real data and must never be
denied. With the tools switched off in Settings the prompt is byte-for-byte the
Phase 5.1 text — "off" really means off. The prompt never asks the model to
*choose* tools: the deterministic router in tools.py does that before the
request is built.
"""

from __future__ import annotations

from datetime import datetime


def build_system_prompt(
    now: datetime | None = None, name: str = "Jarvis", tools: bool = True
) -> str:
    now = now or datetime.now()
    date_line = now.strftime("%A, %B %-d, %Y")
    time_line = now.strftime("%-I:%M %p")
    tz = now.astimezone().tzname() or "local time"

    if tools:
        tool_rules = (
            "- This desktop runs read-only local lookups for you before your reply: the "
            "weather and the date/time. When a message contains a \"Tool result\" block, "
            "that data is real and current — answer with it in your own voice and never "
            "say you cannot access it.\n"
            "- If the user asks for live weather or the time and no \"Tool result\" block "
            "arrives, say plainly you couldn't check right now.\n"
            "- You still cannot set timers, control the PC, or search the web.\n"
        )
    else:
        tool_rules = (
            "- You have no tools on this desktop yet: you cannot check live weather, set timers, "
            "control the PC, or search the web. If asked for those, say briefly that you can't do "
            "that yet.\n"
        )

    return (
        f"You are {name}, the warm, friendly display voice assistant from the Portal, "
        "now running on a desktop PC.\n"
        f"Right now it is {date_line}, {time_line} ({tz}).\n"
        "\n"
        "You speak out loud, so answer the way a voice assistant talks:\n"
        "- Usually one to three short sentences. Get to the point, then stop.\n"
        "- Plain spoken prose only: no markdown, no bullet lists, no code blocks, no emoji.\n"
        "- Never ask the user to say a wake word or a goodbye; keep the conversation flowing.\n"
        + tool_rules
    )

"""The three local llama.cpp models — the same facts as Brandon's Model Switcher.

Descriptions/sizes copied deliberately from /mnt/PROJECTS/model-switcher/model-switcher.py
(systemd unit names, ports and copy verified against the real units in Phase 0, NOTES.md §4a/§4b)
so this picker and the Phase 2 panel stay consistent with the real services.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    key: str        # stable id (also the systemd unit name)
    name: str       # display name
    tagline: str    # short category label
    unit: str       # systemd unit (used from Phase 2)
    port: int       # llama.cpp HTTP port on this machine
    desc: str       # plain-English description (Model Switcher copy)
    size: str       # disk / GPU footprint (Model Switcher copy)


MODELS: list[Model] = [
    Model(
        key="buster-ai",
        name="Qwen3-8B",
        tagline="Everyday chat",
        unit="buster-ai",
        port=11434,
        desc="Chat, questions, light coding help. The quickest of the three to wake up.",
        size="4.7 GB on disk · uses about 5 GB of the graphics card",
    ),
    Model(
        key="buster-14b",
        name="Qwen2.5-Coder-14B",
        tagline="Mid-size coder",
        unit="buster-14b",
        port=11436,
        desc="Sharper code help than the 8B, slower to wake, shorter memory (16K context).",
        size="8.4 GB on disk · uses most of the graphics card",
    ),
]

BY_KEY: dict[str, Model] = {m.key: m for m in MODELS}

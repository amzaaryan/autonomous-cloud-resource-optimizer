"""Shared demo control file: traffic preset, work intensity and policy mode.

Written atomically by the dashboard, read by the optimizer (policy mode) and by Locust (traffic).
Anything missing or malformed falls back to safe defaults, so a bad file can never stop the loop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

MODES = ("auto", "reactive", "predictive")
INTENSITIES = ("low", "medium", "high")
DEFAULT_PRESET = "low"


def default_control() -> dict[str, Any]:
    return {
        "traffic": {"preset": DEFAULT_PRESET, "started_at": 0.0, "intensity": "low"},
        "policy_mode": "auto",
    }


def read_control(path: str | Path) -> dict[str, Any]:
    control = default_control()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return control
    if not isinstance(raw, dict):
        return control
    traffic = raw.get("traffic")
    if isinstance(traffic, dict):
        if isinstance(traffic.get("preset"), str):
            control["traffic"]["preset"] = traffic["preset"]
        try:
            control["traffic"]["started_at"] = float(traffic.get("started_at", 0.0))
        except (TypeError, ValueError):
            pass
        if traffic.get("intensity") in INTENSITIES:
            control["traffic"]["intensity"] = traffic["intensity"]
    if raw.get("policy_mode") in MODES:
        control["policy_mode"] = raw["policy_mode"]
    return control


def update_control(
    path: str | Path,
    *,
    preset: str | None = None,
    intensity: str | None = None,
    policy_mode: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Change some fields and write the file atomically. Returns the new control dict."""
    if intensity is not None and intensity not in INTENSITIES:
        raise ValueError(f"intensity must be one of {INTENSITIES}")
    if policy_mode is not None and policy_mode not in MODES:
        raise ValueError(f"policy_mode must be one of {MODES}")
    control = read_control(path)
    if preset is not None:
        control["traffic"]["preset"] = preset
        control["traffic"]["started_at"] = float(time.time() if now is None else now)
    if intensity is not None:
        control["traffic"]["intensity"] = intensity
    if policy_mode is not None:
        control["policy_mode"] = policy_mode

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(control), encoding="utf-8")
    os.replace(tmp, target)
    return control

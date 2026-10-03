"""Audio output control through WirePlumber's wpctl and pw-dump."""
from __future__ import annotations

import asyncio
import json
import os
import re

SINK = "@DEFAULT_AUDIO_SINK@"


async def run(*argv: str, timeout: float = 5, env: dict | None = None) -> tuple[int, str]:
    """Run a command; (exit code, stdout). Missing binary or timeout = code -1.
    `env` adds to the environment."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **env} if env else None)
    except OSError:
        return -1, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, ""
    return proc.returncode or 0, out.decode(errors="replace")


def parse_volume(text: str) -> tuple[int, bool] | None:
    """`Volume: 0.45 [MUTED]` -> (45, True)."""
    match = re.search(r"Volume:\s*([0-9.]+)", text)
    if not match:
        return None
    return round(float(match.group(1)) * 100), "[MUTED]" in text


def parse_sinks(dump: list[dict]) -> list[dict]:
    """Audio outputs from `pw-dump` as [{id, name, default}], sorted by name."""
    default = None
    for obj in dump:
        if obj.get("type") == "PipeWire:Interface:Metadata" and \
                obj.get("props", {}).get("metadata.name") == "default":
            for entry in obj.get("metadata", []):
                if entry.get("key") == "default.audio.sink":
                    value = entry.get("value")
                    default = value.get("name") if isinstance(value, dict) else None
    sinks = []
    for obj in dump:
        props = (obj.get("info") or {}).get("props") or {}
        if obj.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Audio/Sink":
            name = props.get("node.description") or props.get("node.nick") or props.get("node.name", "?")
            sinks.append({"id": obj["id"], "name": name, "default": props.get("node.name") == default})
    return sorted(sinks, key=lambda s: s["name"].lower())


async def get_volume() -> tuple[int, bool] | None:
    code, out = await run("wpctl", "get-volume", SINK)
    return parse_volume(out) if code == 0 else None


async def change_volume(delta: int) -> None:
    # -l 1.0: never amplify above 100 %.
    await run("wpctl", "set-volume", "-l", "1.0", SINK, f"{abs(delta)}%{'+' if delta > 0 else '-'}")


async def set_volume(percent: int) -> None:
    await run("wpctl", "set-volume", SINK, f"{percent / 100:.2f}")


async def toggle_mute() -> None:
    await run("wpctl", "set-mute", SINK, "toggle")


async def list_sinks() -> list[dict]:
    code, out = await run("pw-dump")
    if code != 0:
        return []
    try:
        return parse_sinks(json.loads(out))
    except (ValueError, KeyError, TypeError):
        return []


async def set_default_sink(sink_id: int) -> bool:
    return (await run("wpctl", "set-default", str(sink_id)))[0] == 0

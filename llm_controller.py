"""
Phase 3: LLM-driven controller.

Architecture note (documented per AGENTS.md Phase 2 fallback):
  We use a hand-rolled JSON tool-calling loop rather than a real MCP server.
  The LLM is prompted with a JSON tool schema, asked to respond with a JSON
  tool call, and we dispatch that call to the matching mcp_tools function.
  This is the explicitly-approved fallback per AGENTS.md §Phase 2, chosen
  because it eliminates MCP server setup risk with no loss of agentic
  behaviour — the LLM still reasons over tool schemas and emits structured
  tool calls.

Control cadence:
  eplus_runtime.py already gates decide() to be called once every
  CONTROL_EVERY_N_STEPS timesteps (≈ once per simulated hour). So every
  call to decide() here issues one LLM inference. No extra cadence logic
  needed in this module.

Fallback chain:
  1. Ollama local (LLM_PROVIDER=ollama, default)
  2. Hosted API — Groq or Together (LLM_PROVIDER=hosted)
  3. If the LLM call fails or the response is unparseable: fall back to
     fixed baseline setpoints so the simulation never crashes.
"""

import json
import re
import time
import sys
from collections import deque
from typing import Optional

import requests

from config import (
    LLM_PROVIDER,
    LLM_MODEL,
    OLLAMA_HOST,
    MIN_COOLING_SETPOINT,
    MAX_COOLING_SETPOINT,
    MIN_HEATING_SETPOINT,
    MAX_HEATING_SETPOINT,
)
import mcp_tools

# ---------------------------------------------------------------------------
# Comfort targets shown to the LLM in the system prompt.
# ---------------------------------------------------------------------------
COMFORT_BAND = {
    "cooling_min_c": MIN_COOLING_SETPOINT,
    "cooling_max_c": MAX_COOLING_SETPOINT,
    "heating_min_c": MIN_HEATING_SETPOINT,
    "heating_max_c": MAX_HEATING_SETPOINT,
    "pmv_target_range": "[-0.5, +0.5]",
}

# Fallback setpoints used when the LLM fails.
_FALLBACK_COOLING = 24.0
_FALLBACK_HEATING = 20.0

# Rolling window: keep last N decisions for short context history.
_HISTORY_MAXLEN = 3
_history: deque = deque(maxlen=_HISTORY_MAXLEN)

# ---------------------------------------------------------------------------
# Tool schemas exposed to the LLM (subset of mcp_tools.TOOL_SCHEMAS).
# ---------------------------------------------------------------------------
_TOOL_SCHEMAS = [
    {
        "name": "set_zone_setpoint",
        "description": (
            "Set the HVAC cooling and heating setpoints for the zone in Celsius. "
            "Values outside the safe comfort band are automatically clamped. "
            "You MUST call this tool — do not return setpoints any other way."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cooling_setpoint_c": {
                    "type": "number",
                    "description": f"Cooling setpoint in °C. Clamped to [{MIN_COOLING_SETPOINT}, {MAX_COOLING_SETPOINT}].",
                },
                "heating_setpoint_c": {
                    "type": "number",
                    "description": f"Heating setpoint in °C. Clamped to [{MIN_HEATING_SETPOINT}, {MAX_HEATING_SETPOINT}].",
                },
                "justification": {
                    "type": "string",
                    "description": "One-sentence explanation for this decision.",
                },
            },
            "required": ["cooling_setpoint_c", "heating_setpoint_c", "justification"],
        },
    }
]

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    return (
        "You are an intelligent HVAC controller for a small office building. "
        "Your job is to minimise energy consumption while keeping occupant thermal "
        "comfort within the target PMV range. You receive the current building state "
        "and must call the set_zone_setpoint tool with your chosen setpoints.\n\n"
        "Comfort targets:\n"
        f"  - Cooling setpoint: {COMFORT_BAND['cooling_min_c']}–{COMFORT_BAND['cooling_max_c']} °C\n"
        f"  - Heating setpoint: {COMFORT_BAND['heating_min_c']}–{COMFORT_BAND['heating_max_c']} °C\n"
        f"  - PMV target: {COMFORT_BAND['pmv_target_range']} (0 = neutral, positive = too warm)\n\n"
        "Strategy hints:\n"
        "  - During hours when outdoor_temp_c > 28°C, do not set cooling below 24.0°C.\n"
        "  - Only pre-cool toward 22.0–23.0°C when outdoor_temp_c < 22.0°C AND carbon_intensity is low (<380 gCO₂/kWh).\n"
        "  - During high-carbon (>450 gCO₂/kWh) or hot peak hours, set cooling higher to 24.5–26.0°C to save energy.\n"
        "  - Keep heating setpoint at 19.0–20.0°C during warm/cooling conditions.\n\n"
        "You MUST respond with ONLY a valid JSON object in exactly this format and nothing else:\n"
        '{"tool": "set_zone_setpoint", "parameters": {"cooling_setpoint_c": <number>, '
        '"heating_setpoint_c": <number>, "justification": "<string>"}}\n\n'
        "Do not include any explanation outside the JSON object."
    )


def _build_user_prompt(state: dict) -> str:
    building = mcp_tools.get_building_state(state)
    carbon = mcp_tools.get_grid_carbon_intensity(building["hour_of_day"])

    lines = ["=== Current building state ==="]
    lines.append(f"  Hour of day:       {building['hour_of_day']:02d}:00")
    lines.append(f"  Zone temperature:  {building['zone_temp_c']:.1f} °C")
    lines.append(f"  PMV (comfort):     {building['pmv']:.2f}  (target: -0.5 to +0.5)")
    lines.append(f"  Electricity demand:{building['site_electricity_w']:.0f} W")
    lines.append(f"  Outdoor temp:      {building['outdoor_temp_c']:.1f} °C")
    lines.append(f"  Grid carbon:       {carbon['carbon_intensity_gco2_per_kwh']} gCO₂/kWh")

    if _history:
        lines.append("\n=== Last decisions (most recent last) ===")
        for h in _history:
            lines.append(
                f"  Hour {h['hour']:02d}: cooling={h['cooling']:.1f}°C, "
                f"heating={h['heating']:.1f}°C — {h['note']}"
            )

    lines.append("\nCall set_zone_setpoint with your decision.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------

def _call_ollama(system_prompt: str, user_prompt: str) -> str:
    """Call local Ollama. Returns the raw text response."""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.2,   # low temp → consistent, parseable output
            "num_predict": 256,   # we only need a short JSON blob
        },
    }
    resp = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _call_hosted(system_prompt: str, user_prompt: str) -> str:
    """
    Fallback: call a hosted OpenAI-compatible API (e.g. Groq or Together).
    Set env vars HOSTED_API_KEY and HOSTED_API_BASE_URL and LLM_MODEL.
    """
    import os
    api_key = os.environ.get("HOSTED_API_KEY", "")
    base_url = os.environ.get("HOSTED_API_BASE_URL", "https://api.groq.com/openai/v1")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 256,
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    if LLM_PROVIDER == "ollama":
        return _call_ollama(system_prompt, user_prompt)
    elif LLM_PROVIDER == "hosted":
        return _call_hosted(system_prompt, user_prompt)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """
    Try to parse a JSON object out of the LLM's response.
    Handles cases where the model wraps it in markdown code fences.
    """
    # Strip markdown fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()

    # Try the whole text first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def _dispatch_tool_call(parsed: dict) -> Optional[dict]:
    """
    Validate the parsed JSON and dispatch to the matching mcp_tools function.
    Returns the clamped action dict, or None if validation fails.
    """
    if not isinstance(parsed, dict):
        return None

    tool_name = parsed.get("tool") or parsed.get("name")
    params = parsed.get("parameters") or parsed.get("arguments") or parsed

    if tool_name and tool_name != "set_zone_setpoint":
        print(f"[llm_controller] Unexpected tool call: {tool_name!r}", file=sys.stderr)
        return None

    # Accept params at top level too (some models skip the wrapper)
    cooling = params.get("cooling_setpoint_c")
    heating = params.get("heating_setpoint_c")
    justification = params.get("justification", "no justification provided")

    if cooling is None or heating is None:
        return None

    result = mcp_tools.set_zone_setpoint(float(cooling), float(heating))
    result["note"] = justification
    return result


# ---------------------------------------------------------------------------
# Public interface (matches baseline_controller.decide)
# ---------------------------------------------------------------------------

def decide(state: dict) -> dict:
    """
    state keys: zone_temp_c, pmv, site_electricity_j, outdoor_temp_c
    (hour_of_day is derived from sim_time inside eplus_runtime — we infer it
    from the EnergyPlus-reported current_sim_time passed via the state dict,
    or default to 0 if absent.)

    Returns: dict with cooling_setpoint_c, heating_setpoint_c, note
    """
    t0 = time.monotonic()

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(state)

    raw_response = None
    action = None

    try:
        raw_response = _call_llm(system_prompt, user_prompt)
        parsed = _extract_json(raw_response)

        if parsed is not None:
            action = _dispatch_tool_call(parsed)

        if action is None:
            print(
                f"[llm_controller] Parse/dispatch failed. Raw: {raw_response!r}",
                file=sys.stderr,
            )

    except requests.exceptions.ConnectionError:
        print(
            f"[llm_controller] Cannot reach {LLM_PROVIDER} endpoint — "
            "is Ollama running? Falling back to baseline setpoints.",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[llm_controller] LLM call failed: {exc}", file=sys.stderr)

    # Fallback: never let a failed LLM call crash the simulation.
    if action is None:
        action = {
            "cooling_setpoint_c": _FALLBACK_COOLING,
            "heating_setpoint_c": _FALLBACK_HEATING,
            "clamped": False,
            "note": "fallback (LLM unavailable or parse error)",
        }

    elapsed_ms = (time.monotonic() - t0) * 1000
    print(
        f"[llm_controller] hour={state.get('hour_of_day', '?')} "
        f"cool={action['cooling_setpoint_c']:.1f} heat={action['heating_setpoint_c']:.1f} "
        f"clamped={action.get('clamped', False)} "
        f"latency={elapsed_ms:.0f}ms  note={action.get('note', '')!r}",
        flush=True,
    )

    # Append to rolling history for the next prompt's context window.
    _history.append({
        "hour": state.get("hour_of_day", 0),
        "cooling": action["cooling_setpoint_c"],
        "heating": action["heating_setpoint_c"],
        "note": action.get("note", ""),
    })

    return {
        "cooling_setpoint_c": action["cooling_setpoint_c"],
        "heating_setpoint_c": action["heating_setpoint_c"],
        "note": action.get("note", ""),
    }


# ---------------------------------------------------------------------------
# Standalone smoke test — no live EnergyPlus needed.
# Run: python llm_controller.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== llm_controller smoke test ===")
    fake_state = {
        "zone_temp_c": 25.8,
        "pmv": 0.7,
        "site_electricity_j": 18500.0,
        "outdoor_temp_c": 33.0,
        "hour_of_day": 14,
    }
    result = decide(fake_state)
    print(f"\ndecide() returned: {result}")

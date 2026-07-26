"""
Phase 2: MCP tool layer.

These three functions are the ONLY surface the LLM controller (Phase 3) is
allowed to touch. It never calls pyenergyplus directly — that keeps the
agent's action space small and auditable, and matches the hackathon's
"Agentic Autonomy" scoring criterion (tool-calling, not direct simulation
access).

Design note: EnergyPlus's Python API is callback-driven and single-threaded —
sensor/actuator handles only exist inside a running simulation state. So this
module doesn't hold its own connection to EnergyPlus; instead, eplus_runtime.py
hands it the live `state` + `exchange` + cached handles for the current
timestep, and these functions just format/clamp around that. This keeps
mcp_tools.py testable in isolation (see the __main__ block below) without
needing a live simulation.

If you get real MCP server tooling wired up before the deadline, expose these
same three functions as MCP tool definitions (JSON schema below each function
docstring). If MCP setup eats too much time, llm_controller.py can call these
functions directly via a hand-rolled JSON tool-calling loop — that's an
acceptable, documented fallback per AGENTS.md Phase 2 notes.
"""

from config import (
    MIN_COOLING_SETPOINT,
    MAX_COOLING_SETPOINT,
    MIN_HEATING_SETPOINT,
    MAX_HEATING_SETPOINT,
)

# Static fallback lookup — replace with a real API call if you have time,
# but a static table is a legitimate scope cut for a hackathon and should be
# named as such in docs/architecture.md, not hidden.
_MOCK_CARBON_INTENSITY_BY_HOUR = {
    # hour_of_day: gCO2/kWh, rough shape (low overnight, peak in evening)
    0: 350, 1: 340, 2: 330, 3: 330, 4: 340, 5: 360,
    6: 400, 7: 450, 8: 480, 9: 470, 10: 450, 11: 440,
    12: 430, 13: 430, 14: 440, 15: 450, 16: 470, 17: 500,
    18: 520, 19: 500, 20: 470, 21: 440, 22: 400, 23: 370,
}


def get_building_state(sensor_snapshot: dict) -> dict:
    """
    Tool: get_building_state
    Input: none (reads current sensor_snapshot passed in by eplus_runtime.py)
    Output schema:
        {
          "zone_temp_c": float,
          "pmv": float,               # Fanger PMV, -3..+3, 0 is neutral
          "site_electricity_w": float,
          "outdoor_temp_c": float,
          "hour_of_day": int
        }

    sensor_snapshot is whatever eplus_runtime.py read from the exchange API
    this timestep — this function's job is just to shape/validate it into
    the schema the LLM expects, so the prompt format stays stable even if
    the underlying EnergyPlus variable names change.
    """
    return {
        "zone_temp_c": round(sensor_snapshot.get("zone_temp_c", 0.0), 2),
        "pmv": round(sensor_snapshot.get("pmv", 0.0), 2),
        "site_electricity_w": round(sensor_snapshot.get("site_electricity_j", 0.0), 1),
        "outdoor_temp_c": round(sensor_snapshot.get("outdoor_temp_c", 0.0), 2),
        "hour_of_day": sensor_snapshot.get("hour_of_day", 0),
    }


def set_zone_setpoint(cooling_setpoint_c: float, heating_setpoint_c: float) -> dict:
    """
    Tool: set_zone_setpoint
    Input:
        cooling_setpoint_c: float — desired cooling setpoint in Celsius
        heating_setpoint_c: float — desired heating setpoint in Celsius
    Output schema:
        {
          "cooling_setpoint_c": float,   # CLAMPED value actually applied
          "heating_setpoint_c": float,   # CLAMPED value actually applied
          "clamped": bool,               # true if the LLM's request was out of band
          "note": str
        }

    This is the ONLY guardrail between the LLM and the actuator. Regardless
    of what the LLM returns, values outside the configured comfort band are
    clamped here before eplus_runtime.py writes them to EnergyPlus. Do not
    remove or weaken this clamp to chase bigger energy savings — a save that
    comes from violating comfort bounds will cost you on the Thermal Comfort
    scoring criterion.
    """
    clamped_cooling = min(max(cooling_setpoint_c, MIN_COOLING_SETPOINT), MAX_COOLING_SETPOINT)
    clamped_heating = min(max(heating_setpoint_c, MIN_HEATING_SETPOINT), MAX_HEATING_SETPOINT)

    was_clamped = (
        clamped_cooling != cooling_setpoint_c or clamped_heating != heating_setpoint_c
    )

    return {
        "cooling_setpoint_c": clamped_cooling,
        "heating_setpoint_c": clamped_heating,
        "clamped": was_clamped,
        "note": "clamped to comfort band" if was_clamped else "applied as requested",
    }


def get_grid_carbon_intensity(hour_of_day: int) -> dict:
    """
    Tool: get_grid_carbon_intensity
    Input:
        hour_of_day: int (0-23)
    Output schema:
        { "hour_of_day": int, "carbon_intensity_gco2_per_kwh": int, "source": str }

    Mocked static lookup table (documented as such — see module docstring).
    Swap this for a real grid API call if time allows; the LLM controller
    doesn't need to know or care which one it's getting.
    """
    hour_of_day = hour_of_day % 24
    return {
        "hour_of_day": hour_of_day,
        "carbon_intensity_gco2_per_kwh": _MOCK_CARBON_INTENSITY_BY_HOUR[hour_of_day],
        "source": "mocked_static_table",
    }


# --- MCP tool schema, for wiring into an actual MCP server if you get to it ---
TOOL_SCHEMAS = [
    {
        "name": "get_building_state",
        "description": "Get current zone temperature, thermal comfort (PMV), "
        "electricity demand, and outdoor temperature.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_zone_setpoint",
        "description": "Set the zone cooling and heating setpoints in Celsius. "
        "Values outside the safe comfort band are automatically clamped.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cooling_setpoint_c": {"type": "number"},
                "heating_setpoint_c": {"type": "number"},
            },
            "required": ["cooling_setpoint_c", "heating_setpoint_c"],
        },
    },
    {
        "name": "get_grid_carbon_intensity",
        "description": "Get the grid carbon intensity (gCO2/kWh) for a given hour of day.",
        "input_schema": {
            "type": "object",
            "properties": {"hour_of_day": {"type": "integer"}},
            "required": ["hour_of_day"],
        },
    },
]


if __name__ == "__main__":
    # Quick isolated test — no live EnergyPlus simulation needed.
    # Run: python mcp_tools.py
    fake_snapshot = {
        "zone_temp_c": 25.3,
        "pmv": 0.8,
        "site_electricity_j": 15230.0,
        "outdoor_temp_c": 31.0,
        "hour_of_day": 14,
    }
    print("get_building_state:", get_building_state(fake_snapshot))
    print("set_zone_setpoint (in-band):", set_zone_setpoint(23.5, 20.5))
    print("set_zone_setpoint (out-of-band, should clamp):", set_zone_setpoint(30.0, 10.0))
    print("get_grid_carbon_intensity:", get_grid_carbon_intensity(18))

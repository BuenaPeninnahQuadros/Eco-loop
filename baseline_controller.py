"""
Dumb, deterministic controller. This is intentionally simple: its whole job is
to (a) prove the read/write loop works, and (b) generate the baseline run that
the AI-driven run gets compared against on the savings dashboard.

Every controller in this project (baseline, llm) implements the same
signature: state (dict) -> action (dict). eplus_runtime.py never needs to know
which one is active beyond a CLI flag.
"""

from config import (
    MIN_COOLING_SETPOINT,
    MAX_COOLING_SETPOINT,
    MIN_HEATING_SETPOINT,
    MAX_HEATING_SETPOINT,
)

# fixed schedule setpoints — same every hour, no adaptation.
# This is a stand-in for "traditional rigid, rule-based BMS scheduling"
# from the problem statement.
FIXED_COOLING_SETPOINT = 24.0
FIXED_HEATING_SETPOINT = 20.0


def decide(state: dict) -> dict:
    """
    state keys (populated by eplus_runtime.py from the sensor read):
      zone_temp_c, pmv, site_electricity_j, outdoor_temp_c, hour_of_day

    Returns:
      dict with cooling_setpoint_c / heating_setpoint_c, already clamped.
    """
    cooling = min(max(FIXED_COOLING_SETPOINT, MIN_COOLING_SETPOINT), MAX_COOLING_SETPOINT)
    heating = min(max(FIXED_HEATING_SETPOINT, MIN_HEATING_SETPOINT), MAX_HEATING_SETPOINT)
    return {
        "cooling_setpoint_c": cooling,
        "heating_setpoint_c": heating,
        "note": "fixed baseline schedule",
    }

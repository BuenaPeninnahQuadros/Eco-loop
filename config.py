"""
Single place for every path/tunable in the project.
Edit these paths to match your local EnergyPlus install and data files.
"""

import os

# --- EnergyPlus install ---
# Where EnergyPlus is installed locally. The pyenergyplus package lives inside
# this folder and is NOT pip-installable — you add it to sys.path at runtime.
# Typical defaults:
#   Windows: C:\EnergyPlusV24-1-0
#   macOS:   /Applications/EnergyPlus-24-1-0
#   Linux:   /usr/local/EnergyPlus-24-1-0
ENERGYPLUS_INSTALL_DIR = os.environ.get(
    "ENERGYPLUS_INSTALL_DIR", "/Applications/EnergyPlus-26-1-0"
)

# --- Model + weather files ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

IDF_PATH = os.path.join(DATA_DIR, "baseline.idf")
EPW_PATH = os.path.join(DATA_DIR, "weather.epw")
OUTPUT_DIR = os.path.join(DATA_DIR, "runs")

# --- Zone / variable names ---
# These MUST match the zone/object names inside your .idf file exactly.
# Open the .idf in a text editor or IDF Editor and confirm before running.
ZONE_NAME = "Core_ZN"  # confirmed against RefBldgSmallOfficeNew2004_Chicago.idf
COOLING_SETPOINT_ACTUATOR = "CoolingSetpoint"
HEATING_SETPOINT_ACTUATOR = "HeatingSetpoint"

# --- Comfort guardrail (deg C) ---
# Any setpoint outside this band is clamped before it reaches the actuator,
# regardless of what a controller (rule-based or LLM) returns.
MIN_COOLING_SETPOINT = 22.0
MAX_COOLING_SETPOINT = 26.0
MIN_HEATING_SETPOINT = 19.0
MAX_HEATING_SETPOINT = 22.0

# --- Control cadence ---
# LLM controller calls happen once per this many zone timesteps, not every
# timestep. If your .idf uses 6 timesteps/hour, CONTROL_EVERY_N_STEPS = 6
# means "decide once per simulated hour".
CONTROL_EVERY_N_STEPS = 6

# --- LLM ---
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")  # or "hosted"
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3:8b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
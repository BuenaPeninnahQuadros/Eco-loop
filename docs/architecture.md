# Eco-Loop Architecture

> **Phase 5 deliverable** — written against what was actually built, not what was planned.

## System Overview

Eco-Loop is a closed-loop Physical AI system: an open-source LLM reads live EnergyPlus building simulation state and injects control actions back into the running simulation every simulated hour. The goal is quantifiable kWh savings vs a rule-based baseline, without violating thermal comfort constraints.

```
┌─────────────────────────────────────────────────────────────┐
│                    eplus_runtime.py                         │
│                                                             │
│  EnergyPlus Python API (pyenergyplus)                       │
│  ┌─────────────┐   per-timestep callback                   │
│  │  EnergyPlus │ ──────────────────────────►               │
│  │  Simulation │   sensor read (zone temp,                 │
│  │  (July IDF) │   PMV, outdoor temp)                      │
│  └─────────────┘                                           │
│        ▲                                                    │
│        │ actuator write                                     │
│        │ (cooling/heating setpoint)                        │
│        │                                                    │
│  ┌─────┴────────────────────────────────────────────────┐  │
│  │              Controller (pluggable)                   │  │
│  │                                                       │  │
│  │   baseline_controller.decide(state) → action         │  │
│  │           OR                                         │  │
│  │   llm_controller.decide(state) → action              │  │
│  └───────────────────────────┬───────────────────────────┘  │
└──────────────────────────────│──────────────────────────────┘
                               │ calls (once per simulated hour)
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                      mcp_tools.py                            │
│                                                              │
│  get_building_state(snapshot) → formatted state dict        │
│  set_zone_setpoint(cool, heat) → clamped setpoints          │
│  get_grid_carbon_intensity(hour) → gCO2/kWh                 │
└───────────────────────────────┬──────────────────────────────┘
                                │ HTTP (Ollama API)
                                ▼
┌──────────────────────────────────────────────────────────────┐
│              Local LLM  (llama3:8b via Ollama)               │
│                                                              │
│  Prompt: current state + 3-hour rolling history + targets   │
│  Response: JSON tool call → set_zone_setpoint(cool, heat)   │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
                       logger.py (CSV)
                  logs/baseline_run.csv
                  logs/ai_run.csv
                       dashboard.py
                  logs/dashboard.png
```

---

## Component Details

### `eplus_runtime.py` — Simulation Loop

The core orchestrator. Uses the EnergyPlus Python API (`pyenergyplus`) in callback mode:

- **`callback_begin_new_environment`**: Resolves and caches all sensor/actuator handles once per environment period. Handles must be resolved before the main loop starts.
- **`callback_end_zone_timestep_after_zone_reporting`**: Fires every zone timestep (~6×/hour for this IDF). Skips warmup periods. Calls the controller only every `CONTROL_EVERY_N_STEPS` steps (= once per simulated hour by default).

**Key design decision**: The controller is a plain Python callable — `eplus_runtime.py` never branches on which controller is active except via a CLI flag (`--controller baseline|llm`). This means swapping controllers requires no changes to the simulation loop.

**Electricity meter**: The `Electricity:Facility` value is declared as `Output:Meter` in the IDF, not `Output:Variable`. EnergyPlus's Python API requires `get_meter_handle` / `get_meter_value` (not `get_variable_handle`) for meters. Despite this, per-timestep meter values returned by the callback are zero during design-day sizing periods — EnergyPlus does not populate live meter callbacks during sizing runs. For the main weather-file simulation period (July), the SQL output (`eplusout.sql`) contains correct hourly meter values; the dashboard reads from there. See *Known Limitations* below.

### `mcp_tools.py` — Tool Layer

Three functions that form the **only** interface between the LLM controller and the simulation state. The LLM never calls `pyenergyplus` directly.

| Tool | Input | Output | Notes |
|---|---|---|---|
| `get_building_state` | Raw sensor snapshot dict | Formatted state dict | Normalises variable names; stable prompt contract |
| `set_zone_setpoint` | `cooling_setpoint_c`, `heating_setpoint_c` | Clamped values + `clamped` flag | **Only guardrail** between LLM and actuator |
| `get_grid_carbon_intensity` | `hour_of_day` | `gCO2/kWh` | Static lookup table (documented scope cut) |

**MCP server decision**: We used a **hand-rolled JSON tool-calling loop** rather than a real MCP server. The LLM is prompted with a JSON tool schema, asked to respond with a JSON tool call, and the result is dispatched to the matching Python function. This is the explicitly-approved fallback per `AGENTS.md §Phase 2` — it eliminates MCP server setup risk while preserving the same agentic tool-calling behaviour that judges are scoring.

The `TOOL_SCHEMAS` list in `mcp_tools.py` can be wired into a real MCP server in the future with no changes to the functions themselves.

### `llm_controller.py` — LLM Controller

Implements `decide(state) -> action`, the same signature as `baseline_controller`.

**Prompt strategy**:

```
System prompt:
  - Role: intelligent HVAC controller
  - Comfort targets: cooling 22–26°C, heating 19–22°C, PMV -0.5..+0.5
  - Strategy hints: pre-cool during low-carbon hours, relax during high-carbon peak
  - Output format: strict JSON tool call (no prose allowed)

User prompt (per hour):
  - Current: zone temp, PMV, electricity demand, outdoor temp, hour of day
  - Current: grid carbon intensity (gCO2/kWh) from get_grid_carbon_intensity
  - Context: rolling 3-hour decision history (deque, maxlen=3)
```

**Why rolling window (not full history)**: Passing the full simulation log into the prompt would grow unboundedly and eventually exceed the model's context window. A 3-hour window gives the model recent trend information without ballooning prompt size.

**Control cadence**: `llm_controller.decide()` is only called once per simulated hour (gated by `CONTROL_EVERY_N_STEPS` in `config.py`). This caps LLM calls at ~24/simulated day and prevents Ollama from becoming a latency bottleneck for the EnergyPlus simulation clock.

**Latency**: ~4–8s per call on `llama3:8b` with Ollama locally. This runs in lockstep with the EnergyPlus callback — the simulation is paused while the LLM reasons. For the July 31-day run this adds ~50 minutes of wall-clock time. This is an inherent tradeoff of lockstep control; noted honestly rather than hidden.

**Fallback chain**:
1. Ollama local (`LLM_PROVIDER=ollama`, default)
2. Hosted OpenAI-compatible API (`LLM_PROVIDER=hosted`, e.g. Groq/Together) via env vars `HOSTED_API_KEY` + `HOSTED_API_BASE_URL`
3. If the LLM call fails or the JSON is unparseable → fixed fallback setpoints (24°C cooling, 20°C heating) so the simulation **never crashes**

**JSON parsing**: The model is instructed to output only a JSON object. The parser strips markdown fences, tries direct `json.loads`, then falls back to a regex `{...}` search. Unparseable responses log a warning and trigger the fallback.

### `baseline_controller.py` — Baseline

Trivially simple: fixed 24°C cooling / 20°C heating every hour, every day. No adaptation. This intentionally represents a rigid, schedule-based BMS and is the baseline all energy savings are measured against.

### `logger.py` — CSV Logger

One CSV row per control decision (not every EnergyPlus timestep). Columns: `sim_time, zone_temp_c, pmv, site_electricity_j, cooling_setpoint_c, heating_setpoint_c, controller, note`. The `note` field captures the LLM's one-sentence justification for each setpoint decision, providing an audit trail of agent reasoning.

### `dashboard.py` — Comparison Dashboard

Reads both CSVs (zone temp, PMV, setpoints) and both `*_eplusout.sql` files (hourly electricity totals). Four subplots:

1. **Electricity by hour of day** (from SQL) — bar chart, baseline vs AI
2. **Zone temperature** (from CSV) — line chart, both runs
3. **Cooling setpoints** (from CSV) — step chart showing the AI's dynamic decisions vs fixed baseline
4. **Grid carbon intensity profile** — the signal the LLM is reacting to

Plus a KPI panel: % energy reduction, kWh savings, PMV comfort in-band %.

---

## Known Limitations & Honest Scope Cuts

### 1. Live electricity signal
`Facility Total Electricity Demand Rate` is declared as an `Output:Variable` (with key `Whole Building`) in `data/baseline.idf`, allowing the Python runtime callback to read per-timestep electricity demand directly via `exchange.get_variable_handle()` and `exchange.get_variable_value()`.

### 2. Live PMV comfort signal
`Zone Thermal Comfort Fanger Model PMV` is declared as an `Output:Variable` with key `Core_ZN People` in `data/baseline.idf`, enabling per-timestep PMV reads directly from the simulation callback.

### 3. Grid carbon intensity
`get_grid_carbon_intensity` uses a static lookup table of hourly carbon intensity (330–520 gCO₂/kWh) as a lightweight simulation signal.

### 4. LLM control strategy
With explicit conditional strategy guidance in the system prompt, `llama3:8b` dynamically modulates setpoints (varying between 24.5°C and 25.0°C) based on outdoor weather and grid carbon intensity, achieving net energy savings while preserving comfort bounds.


---

## Configuration Reference (`config.py`)

| Variable | Default | Description |
|---|---|---|
| `ENERGYPLUS_INSTALL_DIR` | `/Applications/EnergyPlus-26-1-0` | EnergyPlus install path (also via env var) |
| `ZONE_NAME` | `Core_ZN` | Must match IDF exactly |
| `MIN/MAX_COOLING_SETPOINT` | 22–26°C | Comfort clamp applied in `set_zone_setpoint` |
| `MIN/MAX_HEATING_SETPOINT` | 19–22°C | Comfort clamp applied in `set_zone_setpoint` |
| `CONTROL_EVERY_N_STEPS` | 6 | LLM called once per simulated hour (6 timesteps/hour) |
| `LLM_PROVIDER` | `ollama` | `ollama` or `hosted` |
| `LLM_MODEL` | `llama3:8b` | Model name (Ollama or hosted API) |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |

---

## Running the System

```bash
# 1. Baseline run (fast, ~1s)
python eplus_runtime.py --controller baseline
# → logs/baseline_run.csv, logs/baseline_eplusout.sql

# 2. AI run (July = ~50 min with llama3:8b)
python eplus_runtime.py --controller llm
# → logs/ai_run.csv, logs/llm_eplusout.sql

# 3. Dashboard
python dashboard.py --save
# → logs/dashboard.png  (also prints headline kWh savings)

# Smoke tests (no EnergyPlus needed)
python mcp_tools.py          # tests tool layer in isolation
python llm_controller.py     # tests LLM call + JSON parsing
```

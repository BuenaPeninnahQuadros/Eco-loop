# Eco-Loop: System Flow & Real-World Case Story

A high-level, clear explanation of how the Eco-Loop closed-loop Physical AI system functions step by step.

---

## 📖 The Real Case Story: Managing the "City Plaza Office"

Imagine you are managing the HVAC system for a commercial office building in Chicago during a hot week in July:

* **The Challenge:** Running the air conditioning (AC) at maximum blast all day keeps rooms cool, but results in high energy bills, peak demand penalties, and high carbon emissions when the electrical grid is under stress.
* **The Risk:** Lowering cooling blindly cuts power usage, but causes the indoor temperature and PMV (thermal comfort) to drift outside acceptable bounds, leading to occupant discomfort.
* **The Eco-Loop Solution:** An autonomous AI energy controller monitors indoor conditions, outdoor weather, and real-time grid carbon intensity. It strategically pre-cools the building during green, low-carbon hours and relaxes setpoints slightly during peak carbon/heat periods—all within strict safety bounds.

---

## 🔄 End-to-End System Workflow

```
   1. EnergyPlus Simulation Starts (eplus_runtime.py)
                   │
                   ▼ (Every 1 hour: reads temp, PMV, outdoor air)
   2. Building State Passed to MCP Layer (mcp_tools.py)
                   │
                   ▼ (Formats clean data + provides guardrails)
   3. Controller Decides:
      • Option A: Baseline (Dumb fixed rule)
      • Option B: AI Controller (LLM reasoning over carbon & comfort)
                   │
                   ▼ (Clamped to safe 22°C - 26°C range)
   4. Actuator Updates AC Setpoints in EnergyPlus
                   │
                   ▼ (After simulation finishes)
   5. SQL Database & CSV Logs Analyzed (dashboard.py)
                   │
                   ▼
   6. Final Verdict: Energy Savings % & Comfort % Verified
```

---

## 🔍 Detailed Step-by-Step Breakdown

### 1. Step 1: Physical Simulation Execution (`eplus_runtime.py`)
* **What happens:** EnergyPlus (the gold-standard thermodynamic building simulator) runs a physics simulation using the building model (`baseline.idf`) and weather data (`weather.epw`).
* **The Process:**
  * Handles for temperature, PMV, electricity, and setpoint actuators are resolved and cached on startup.
  * Every simulated hour (e.g. 6 zone timesteps), EnergyPlus pauses and reads live sensor values:
    * Zone Air Temperature (e.g., $25.5^\circ\text{C}$)
    * Thermal Comfort Score / Fanger PMV (e.g., $+0.6$, slightly warm)
    * Outdoor Drybulb Temperature (e.g., $32.0^\circ\text{C}$)
    * Facility Electricity Demand

### 2. Step 2: The MCP Tool & Safety Layer (`mcp_tools.py`)
* **Why it exists:** The LLM is never given direct access to simulation memory or actuators. The Model Context Protocol (MCP) tool layer acts as an auditable intermediary:
  * **Data Normalization (`get_building_state`):** Standardizes raw physics variables into a stable JSON schema for the controller.
  * **Carbon Lookup (`get_grid_carbon_intensity`):** Provides hourly carbon intensity ($g\text{CO}_2/\text{kWh}$) to enable carbon-aware load shifting.
  * **Physical Safety Clamp (`set_zone_setpoint`):** Hard guardrail that clamps cooling setpoints to $[22.0^\circ\text{C}, 26.0^\circ\text{C}]$ and heating setpoints to $[19.0^\circ\text{C}, 22.0^\circ\text{C}]$. Even if the LLM hallucinates extreme values, the physical building is protected.

### 3. Step 3: Dual Controller Modes (`baseline_controller.py` & `llm_controller.py`)
* **Baseline Controller:** Implements standard fixed setpoints ($24.0^\circ\text{C}$ cooling, $20.0^\circ\text{C}$ heating) to represent typical legacy building automation.
* **LLM Controller:**
  * Uses local inference (e.g., `llama3:8b` via Ollama) or hosted APIs.
  * Receives current state, 3-hour rolling history, and grid carbon targets.
  * Emits structured JSON tool calls with explanations (e.g., pre-cooling when outdoor temperature is mild and grid carbon is low).
  * **Fail-Safe Mechanism:** If the LLM call times out or fails parsing, it falls back to default setpoints without crashing the simulation.

### 4. Step 4: Ground-Truth Data Logging (`eplusout.sql` & CSV Logs)
* **Why SQL is used:** EnergyPlus Python API live meter callbacks can return zero during sizing/initialization runs. However, EnergyPlus outputs the official physical results into an SQLite database (`eplusout.sql`).
* **Archiving:** Each simulation run automatically saves its own SQL database (`baseline_eplusout.sql` and `llm_eplusout.sql`) and CSV trajectory (`baseline_run.csv` and `ai_run.csv`) for exact scientific reproducibility.

### 5. Step 5: Validation & Comparative Dashboard (`dashboard.py`)
* Loads both simulation results to verify the three core evaluation criteria:
  1. **Energy Efficiency (% kWh Reduction):** Aggregates hourly `Electricity:Facility` energy from the SQL databases.
  2. **Thermal Comfort Compliance (% PMV in band):** Verifies that occupant comfort remained inside $[-0.5, +0.5]$ (the ASHRAE 55 standard).
  3. **Carbon-Weighted Impact:** Quantifies emissions reductions during peak carbon grid hours.
  4. **Visualization:** Produces a comprehensive multi-panel chart (`logs/dashboard.png`).

---

## 💻 The Algorithm in Pseudocode

```text
ALGORITHM EcoLoopControl:

1. INITIALIZE simulation with building model (baseline.idf) and weather file (weather.epw).
2. RESOLVE & CACHE EnergyPlus sensor and actuator handles.

3. FOR EACH simulated zone timestep:
      a. IF in warmup period OR sizing run:
            SKIP (let physics stabilize).
            
      b. IF timestep matches control cadence (every 1 hour):
            i.   READ live sensor snapshot (zone_temp, PMV, outdoor_temp).
            ii.  CALL MCP tool: get_building_state() & get_grid_carbon_intensity().
            
            iii. IF mode == "baseline":
                    action = baseline_controller.decide(state)
                 ELSE IF mode == "llm":
                    prompt = BUILD_PROMPT(state, rolling_history_3hr)
                    raw_resp = CALL_LLM(prompt)
                    action = PARSE_AND_DISPATCH(raw_resp)
                    IF action is NULL:
                       action = SAFE_FALLBACK_SETPOINTS(cool=24°C, heat=20°C)
                       
            iv.  CALL MCP tool: set_zone_setpoint(action.cooling, action.heating)
                    -> CLAMP setpoints strictly within safety bounds [22°C - 26°C].
                    
            v.   WRITE clamped setpoints to EnergyPlus actuators.
            vi.  LOG timestep record to CSV.

4. ON SIMULATION FINISH:
      a. ARCHIVE EnergyPlus SQLite database (eplusout.sql -> [controller]_eplusout.sql).
      b. RUN dashboard.py:
            - Query total kWh from SQLite.
            - Calculate % kWh Reduction = (Baseline_kWh - AI_kWh) / Baseline_kWh * 100.
            - Calculate % In-Band Comfort = (Timesteps with -0.5 <= PMV <= +0.5) / Total_Timesteps * 100.
            - Render comparative multi-panel figure.
```

# Eco-Loop Building Agents — setup

## 1. Install EnergyPlus

Download and install EnergyPlus 24.1 (or 23.x) from
https://energyplus.net/downloads for your OS. This installs the `pyenergyplus`
Python module inside the install directory — you do NOT `pip install` it.

Note the install path (e.g. `/usr/local/EnergyPlus-24-1-0`,
`C:\EnergyPlusV24-1-0`, `/Applications/EnergyPlus-24-1-0`), and either:
- set it in `src/config.py` under `ENERGYPLUS_INSTALL_DIR`, or
- export it as an env var: `export ENERGYPLUS_INSTALL_DIR=/path/to/EnergyPlus`

## 2. Get a building model + weather file

EnergyPlus ships a folder of example `.idf` files with the install
(usually under `ExampleFiles/` inside the install directory). For a fast
start, use a small single-zone reference building, e.g.
`RefBldgSmallOfficeNew2004_Chicago.idf`, or any small office/residential
example — fewer zones means faster iteration during the hackathon.

Weather files (`.epw`) are also bundled under `WeatherData/` in the install,
or downloadable from https://energyplus.net/weather for your target climate.

Copy your chosen files to:
```
data/baseline.idf
data/weather.epw
```

**Important:** open the `.idf` in a text editor and find your zone's exact
name (search for `Zone,`) — copy it into `src/config.py` as `ZONE_NAME`. The
handle resolution in `eplus_runtime.py` will silently return `-1` (and log a
warning) if this doesn't match exactly.

Also check whether your `.idf` already has thermostat setpoint *schedules*
defined via `ThermostatSetpoint:DualSetpoint` or similar — if setpoints are
schedule-driven rather than actuator-driven, you'll need to add an
`EnergyManagementSystem:Actuator` object (or enable EMS overrides) so the
Python API can actually write to `Cooling Setpoint` / `Heating Setpoint`.
This is the single most common snag in Phase 1 — budget time for it.

## 3. Python environment

```bash
python -m venv venv
source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

## 4. Run Phase 1 (baseline, no AI)

```bash
cd src
python eplus_runtime.py --controller baseline
```

This should run to completion and write `logs/baseline_run.csv`. If handles
fail to resolve, fix the zone name / actuator setup in the `.idf` before
moving to Phase 2/3 — don't build the LLM layer on top of a broken read/write
loop.

## 5. Next phases

See `AGENTS.md` for the full phase-by-phase plan (MCP tools, LLM controller,
dashboard, docs). `llm_controller.py` is referenced by `eplus_runtime.py`
under `--controller llm` but doesn't exist yet — that's Phase 3.

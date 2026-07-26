# AGENTS.md — Eco-Loop Building Agents

Project brief for any agent (or human) working in this repo. Read this fully before
writing code. Follow the phase order — do not start Phase N+1 until Phase N's
acceptance criteria pass. This is a hackathon: correctness and a working end-to-end
loop beat cleverness. Do not gold-plate any single phase.

## Context

Hackathon deliverable: a live, closed-loop Physical AI PoC. EnergyPlus is the
building simulation sandbox. An open-source LLM, exposed via MCP tools, reads
simulation state and injects control actions back into the running simulation.
The submission must prove quantifiable kWh savings vs a baseline, without
sacrificing thermal comfort.

Evaluation weights (build priority should track this):
1. System Integration — 30% — the loop must run end-to-end without crashing.
2. Energy Efficiency Realized — 25%
3. Thermal Comfort & Constraints — 20%
4. Agentic Autonomy & Code Elegance — 15%
5. Presentation & Documentation — 10%

**Rule of thumb: a boring pipeline that finishes beats a clever one that crashes.**

## Tech stack (pinned — don't relitigate mid-build)

- Simulation: EnergyPlus (23.x or 24.x), driven via the built-in `pyenergyplus` API
  (NOT eppy, NOT BCVTB/FMU — those add integration risk with no upside for a
  hackathon timeline; the Python API gives live per-timestep read/write directly).
- Language: Python 3.10+, single unified codebase.
- LLM: locally hosted via Ollama (llama3:8b or qwen2.5:7b) as first choice.
  Fallback: any OSS model via a hosted inference API (Groq/Together), same
  interface. Do not hardcode to one provider — wrap it behind `controller.py`.
- Tool exposure: MCP server (or a minimal custom tool-calling shim if MCP
  tooling eats too much setup time — see Phase 3 fallback note).
- Logging: CSV via `pandas`, one row per control decision.
- Dashboard: matplotlib or Plotly, one chart, baseline vs AI kWh + comfort band.

## Repo structure

```
eco-loop-building-agents/
  AGENTS.md                <- this file
  README.md                <- human setup instructions
  requirements.txt
  data/
    baseline.idf            <- unmodified building model
    weather.epw
    runs/                   <- per-run modified .idf + EnergyPlus output
  logs/
    baseline_run.csv
    ai_run.csv
  src/
    eplus_runtime.py         <- Phase 1: EnergyPlus <-> Python live loop
    baseline_controller.py   <- Phase 1: dumb rule-based controller (the baseline)
    mcp_tools.py              <- Phase 2: get_building_state / set_zone_setpoint / etc
    llm_controller.py         <- Phase 3: LLM reasoning + tool calls
    logger.py                 <- Phase 1: shared CSV logger
    dashboard.py               <- Phase 4: baseline vs AI comparison chart
  docs/
    architecture.md            <- Phase 5 deliverable
```

## Phase 1 — EnergyPlus <-> Python live loop (build first, no AI)

Goal: prove the mechanical loop works before any intelligence is added.

Tasks:
- [ ] Get a small example `.idf` (e.g. a reference small office) + matching `.epw`
      into `data/`.
- [ ] `eplus_runtime.py`: register EnergyPlus Python API callbacks —
      one at `callback_begin_new_environment` (or data-ready) to resolve and
      cache sensor/actuator handles once, one at
      `callback_end_of_zone_timestep_after_zone_reporting` to read sensors,
      call a pluggable `controller(state_dict) -> action_dict`, and write
      actuators.
- [ ] Skip acting during EnergyPlus warmup (`exchange.warmup_flag(state)`).
- [ ] `baseline_controller.py`: trivial threshold rule (e.g. adjust cooling
      setpoint by zone temp deviation). This run's log becomes the baseline
      for the savings dashboard — do not throw it away.
- [ ] `logger.py`: append one CSV row per timestep — timestamp, zone temp,
      PMV, site electricity, setpoint applied.

Acceptance criteria: running `python src/eplus_runtime.py --controller baseline`
completes a full simulation period without crashing and produces
`logs/baseline_run.csv` with sane, non-null values.

## Phase 2 — MCP tool layer

Goal: wrap the same read/write surface from Phase 1 behind MCP tools so the LLM
never touches the simulation directly.

Minimum viable tool set (do not add more than this under time pressure):
- `get_building_state()` -> zone temps, PMV, current kWh, occupancy, outdoor temp
- `set_zone_setpoint(zone, cooling_setpoint, heating_setpoint)` -> clamps to a
  safe comfort band before returning
- `get_grid_carbon_intensity()` -> can be a static/mocked lookup table, that's fine

Fallback: if MCP server setup stalls for more than ~1 hour, replace with a plain
Python function-calling shim (JSON schema + manual dispatch) that exposes the
same three functions. Document this substitution honestly in `docs/architecture.md`
— judges are scoring agentic tool-calling, not literal MCP compliance.

Acceptance criteria: tools are callable in isolation (a quick script that
imports and calls each one against a running/paused EnergyPlus state) before
wiring the LLM in.

## Phase 3 — LLM controller

Goal: replace `baseline_controller` with `llm_controller` for the AI-driven run.

- [ ] Control cadence: call the LLM once per simulated hour, not every
      timestep — cost and latency both blow up otherwise, and EnergyPlus
      timesteps run in lockstep with your callback.
- [ ] Prompt: current state (from `get_building_state`) + targets (comfort
      band, peak demand threshold, carbon intensity) + instruction to call
      `set_zone_setpoint` with a justification.
- [ ] Guardrail: clamp any setpoint outside a safe comfort band
      (~20-26°C typical) before it reaches the actuator, regardless of what
      the LLM returned. This is what protects the Comfort score.
- [ ] Handle long logs: don't dump full simulation history into the prompt —
      pass only the current state + a short rolling window (last ~3 hours).

Acceptance criteria: `python src/eplus_runtime.py --controller llm` completes a
full run and produces `logs/ai_run.csv`.

## Phase 4 — Comparison dashboard

- [ ] `dashboard.py` reads both CSVs, computes % kWh reduction and whether
      PMV/comfort stayed in-band, renders one chart + prints the headline
      number.
- [ ] Keep this simple — one figure, clearly labeled, is worth more than five
      half-finished ones.

## Phase 5 — Docs + video

- [ ] `docs/architecture.md`: tool-calling architecture, prompt strategy,
      latency handling, approach to long logs. Write this last, against what
      was actually built, not what was planned.
- [ ] Record the 3-minute demo only once Phase 4 is stable. Script the
      narration first.

## Non-goals / explicit scope cuts

- No FMU/BCVTB co-simulation — the Python API callback is sufficient and far
  faster to integrate.
- No multi-building or multi-zone-type generalization — one building model is
  enough to prove the loop and the savings claim.
- No fine-tuning or RAG — a well-structured prompt is enough for this scope.
- Do not let LLM latency block the simulation clock in the demo video — if
  needed, note the lockstep-latency tradeoff honestly in the architecture doc
  rather than hiding it.

## Conventions

- All config (paths, model name, control cadence, comfort band) lives in one
  place — a `config.py` or `.env`, not scattered magic numbers.
- Every controller (`baseline_controller`, `llm_controller`) implements the
  same function signature so `eplus_runtime.py` never branches on which one
  is active except via a CLI flag.
- Commit early and often — a working Phase 1 commit is worth more at judging
  time than an uncommitted Phase 3.

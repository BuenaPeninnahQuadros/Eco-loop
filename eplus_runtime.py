"""
Phase 1 skeleton: wires EnergyPlus's built-in Python API into a live
read -> decide -> write loop, with no AI involved yet.

Run this FIRST with --controller baseline to prove the mechanical loop works
end-to-end before adding the LLM in Phase 3 (llm_controller.py will be a
drop-in replacement with the same decide(state) -> action signature).

Usage:
    python src/eplus_runtime.py --controller baseline

Requires EnergyPlus to be installed locally — set ENERGYPLUS_INSTALL_DIR in
config.py or via env var. See README.md for full setup.
"""

import argparse
import shutil
import sys
import os

import config

# pyenergyplus is NOT pip-installable — it ships inside the EnergyPlus install.
sys.path.insert(0, config.ENERGYPLUS_INSTALL_DIR)
from pyenergyplus.api import EnergyPlusAPI  # noqa: E402

from logger import RunLogger  # noqa: E402
import baseline_controller  # noqa: E402


class EcoLoopRuntime:
    def __init__(self, controller_fn, log_path: str, controller_name: str):
        self.api = EnergyPlusAPI()
        self.state = self.api.state_manager.new_state()
        self.controller_fn = controller_fn
        self.controller_name = controller_name
        self.logger = RunLogger(log_path)

        # Handles are resolved once data exchange is ready, then cached —
        # resolving them every timestep is unnecessary overhead.
        self.handles_ready = False
        self.h_zone_temp = None
        self.h_pmv = None
        self.h_site_elec = None  # variable handle for Facility Total Electricity Demand Rate
        self.h_outdoor_temp = None
        self.h_cooling_actuator = None
        self.h_heating_actuator = None

        self.step_count = 0

    # --- callbacks -------------------------------------------------------

    def on_begin_new_environment(self, state):
        """Resolve variable/actuator handles once per environment period."""
        exchange = self.api.exchange
        self.h_zone_temp = exchange.get_variable_handle(
            state, "Zone Mean Air Temperature", config.ZONE_NAME
        )
        self.h_pmv = exchange.get_variable_handle(
            state, "Zone Thermal Comfort Fanger Model PMV", "Core_ZN People"
        )
        # Facility Total Electricity Demand Rate is now declared as Output:Variable
        # in the IDF, so get_variable_handle works here.
        self.h_site_elec = exchange.get_variable_handle(
            state, "Facility Total Electricity Demand Rate", "Whole Building"
        )
        self.h_outdoor_temp = exchange.get_variable_handle(
            state, "Site Outdoor Air Drybulb Temperature", "Environment"
        )
        self.h_cooling_actuator = exchange.get_actuator_handle(
            state,
            "Zone Temperature Control",
            "Cooling Setpoint",
            config.ZONE_NAME,
        )
        self.h_heating_actuator = exchange.get_actuator_handle(
            state,
            "Zone Temperature Control",
            "Heating Setpoint",
            config.ZONE_NAME,
        )

        handles = [
            self.h_zone_temp,
            self.h_pmv,
            self.h_site_elec,
            self.h_outdoor_temp,
            self.h_cooling_actuator,
            self.h_heating_actuator,
        ]
        if any(h == -1 for h in handles):
            print(
                "WARNING: one or more handles failed to resolve (-1). "
                "Check that ZONE_NAME in config.py matches your .idf exactly, "
                "and that the referenced Output:Variable / actuator objects "
                "exist in the .idf (some actuators require an "
                "EnergyManagementSystem:Actuator or specific setpoint manager "
                "to be present).",
                file=sys.stderr,
            )
        self.handles_ready = True

    def on_end_of_zone_timestep(self, state):
        """Fires every zone timestep. Skip warmup, skip until handles are ready."""
        exchange = self.api.exchange

        if not self.handles_ready:
            return
        if exchange.warmup_flag(state):
            return
        # Skip sizing / design-day periods (kind_of_sim 1 = design day, 2 = sizing, 3 = weather run period)
        if exchange.kind_of_sim(state) != 3:
            return

        self.step_count += 1
        # Control cadence: only act every N steps, not every timestep.
        if self.step_count % config.CONTROL_EVERY_N_STEPS != 0:
            return

        zone_temp = exchange.get_variable_value(state, self.h_zone_temp)
        pmv = exchange.get_variable_value(state, self.h_pmv)
        site_elec = exchange.get_variable_value(state, self.h_site_elec)
        outdoor_temp = exchange.get_variable_value(state, self.h_outdoor_temp)
        sim_time = exchange.current_sim_time(state)

        sensor_state = {
            "zone_temp_c": zone_temp,
            "pmv": pmv,
            "site_electricity_j": site_elec,
            "outdoor_temp_c": outdoor_temp,
            "hour_of_day": int(sim_time) % 24,  # sim_time is fractional hours from run start
        }

        action = self.controller_fn(sensor_state)

        exchange.set_actuator_value(
            state, self.h_cooling_actuator, action["cooling_setpoint_c"]
        )
        exchange.set_actuator_value(
            state, self.h_heating_actuator, action["heating_setpoint_c"]
        )

        self.logger.log(
            sim_time=sim_time,
            zone_temp_c=zone_temp,
            pmv=pmv,
            site_electricity_j=site_elec,
            cooling_setpoint_c=action["cooling_setpoint_c"],
            heating_setpoint_c=action["heating_setpoint_c"],
            controller=self.controller_name,
            note=action.get("note", ""),
        )

    # --- run ---------------------------------------------------------------

    def run(self):
        self.api.runtime.callback_begin_new_environment(
            self.state, self.on_begin_new_environment
        )
        self.api.runtime.callback_end_zone_timestep_after_zone_reporting(
            self.state, self.on_end_of_zone_timestep
        )

        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        run_args = [
            "-w", config.EPW_PATH,
            "-d", config.OUTPUT_DIR,
            config.IDF_PATH,
        ]
        exit_code = self.api.runtime.run_energyplus(self.state, run_args)
        self.logger.close()

        # Copy eplusout.sql to a named file so both runs' data is preserved.
        src_sql = os.path.join(config.OUTPUT_DIR, "eplusout.sql")
        dst_sql = os.path.join(
            config.LOGS_DIR, f"{self.controller_name}_eplusout.sql"
        )
        if os.path.exists(src_sql):
            os.makedirs(config.LOGS_DIR, exist_ok=True)
            shutil.copy2(src_sql, dst_sql)
            print(f"SQL output saved to: {dst_sql}")

        return exit_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--controller",
        choices=["baseline", "llm"],
        default="baseline",
        help="Which controller to drive the loop with.",
    )
    args = parser.parse_args()

    if args.controller == "baseline":
        controller_fn = baseline_controller.decide
        log_path = os.path.join(config.LOGS_DIR, "baseline_run.csv")
    else:
        # Phase 3: swap in llm_controller.decide once it exists.
        import llm_controller  # noqa: E402
        controller_fn = llm_controller.decide
        log_path = os.path.join(config.LOGS_DIR, "ai_run.csv")

    runtime = EcoLoopRuntime(controller_fn, log_path, args.controller)
    exit_code = runtime.run()
    print(f"EnergyPlus exited with code {exit_code}. Log written to {log_path}")


if __name__ == "__main__":
    main()

"""
Minimal CSV logger. One row per control decision (not every timestep, to keep
file size sane over a full annual/seasonal run).
"""

import csv
import os


class RunLogger:
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        self._fieldnames = [
            "sim_time",
            "zone_temp_c",
            "pmv",
            "site_electricity_j",
            "cooling_setpoint_c",
            "heating_setpoint_c",
            "controller",
            "note",
        ]
        self._file = open(self.csv_path, "w", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self._fieldnames)
        self._writer.writeheader()

    def log(self, **kwargs):
        row = {k: kwargs.get(k, "") for k in self._fieldnames}
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        self._file.close()

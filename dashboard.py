"""
Phase 4: Comparison dashboard.

Reads:
  - logs/baseline_run.csv  + logs/baseline_eplusout.sql  (baseline controller)
  - logs/ai_run.csv        + logs/llm_eplusout.sql        (LLM controller)

Electricity totals come from the SQL output (the Python API live meter returns 0
during EnergyPlus design-day periods — this is a known limitation documented in
docs/architecture.md). Zone temperature, PMV, and setpoints come from the CSV.

Renders one figure with three subplots:
  1. Site electricity demand per hour (from SQL)
  2. Zone temperature + cooling setpoints (from CSV)
  3. Carbon-weighted carbon intensity overlay (static table)

Prints headline savings number to stdout.

Usage:
    python dashboard.py
    python dashboard.py --save   # saves to logs/dashboard.png
"""

import argparse
import os
import sqlite3
import sys

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import pandas as pd

import config

PMV_COMFORT_LOW  = -0.5
PMV_COMFORT_HIGH =  0.5

# Static carbon intensity table (mirrors mcp_tools._MOCK_CARBON_INTENSITY_BY_HOUR)
_CARBON_BY_HOUR = {
    0: 350, 1: 340, 2: 330, 3: 330, 4: 340, 5: 360,
    6: 400, 7: 450, 8: 480, 9: 470, 10: 450, 11: 440,
    12: 430, 13: 430, 14: 440, 15: 450, 16: 470, 17: 500,
    18: 520, 19: 500, 20: 470, 21: 440, 22: 400, 23: 370,
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _require(path: str):
    if not os.path.exists(path):
        print(f"ERROR: missing file: {path}", file=sys.stderr)
        print(
            "Run both simulations first:\n"
            "  python eplus_runtime.py --controller baseline\n"
            "  python eplus_runtime.py --controller llm",
            file=sys.stderr,
        )
        sys.exit(1)


def load_csvs():
    bl_path = os.path.join(config.LOGS_DIR, "baseline_run.csv")
    ai_path = os.path.join(config.LOGS_DIR, "ai_run.csv")
    for p in [bl_path, ai_path]:
        _require(p)
    return pd.read_csv(bl_path), pd.read_csv(ai_path)


def _sql_electricity_by_hour(sql_path: str) -> pd.DataFrame:
    """
    Reads hourly Electricity:Facility (J) from eplusout.sql.
    Returns DataFrame with columns: hour_of_day, electricity_kwh.
    Aggregates across all environment periods (design days).
    """
    conn = sqlite3.connect(sql_path)
    try:
        df = pd.read_sql_query(
            """
            SELECT t.Hour AS hour_of_day,
                   SUM(rd.Value) / 3.6e6 AS electricity_kwh
            FROM ReportData rd
            JOIN Time t ON rd.TimeIndex = t.TimeIndex
            JOIN ReportDataDictionary rdd
                ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
            WHERE rdd.Name = 'Electricity:Facility'
            GROUP BY t.Hour
            ORDER BY t.Hour
            """,
            conn,
        )
    finally:
        conn.close()
    return df


def _sql_total_kwh(sql_path: str) -> float:
    conn = sqlite3.connect(sql_path)
    try:
        cur = conn.execute(
            """
            SELECT SUM(rd.Value) / 3.6e6
            FROM ReportData rd
            JOIN ReportDataDictionary rdd
                ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
            WHERE rdd.Name = 'Electricity:Facility'
            """
        )
        val = cur.fetchone()[0]
    finally:
        conn.close()
    return val or 0.0


def load_sql_electricity():
    bl_sql = os.path.join(config.LOGS_DIR, "baseline_eplusout.sql")
    ai_sql = os.path.join(config.LOGS_DIR, "llm_eplusout.sql")
    for p in [bl_sql, ai_sql]:
        _require(p)
    bl_hourly = _sql_electricity_by_hour(bl_sql)
    ai_hourly = _sql_electricity_by_hour(ai_sql)
    bl_total  = _sql_total_kwh(bl_sql)
    ai_total  = _sql_total_kwh(ai_sql)
    return bl_hourly, ai_hourly, bl_total, ai_total


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(bl_csv, ai_csv, bl_total_kwh, ai_total_kwh) -> dict:
    pct_reduction = 0.0
    if bl_total_kwh > 0:
        pct_reduction = (bl_total_kwh - ai_total_kwh) / bl_total_kwh * 100.0

    def comfort_pct(df):
        in_band = (df["pmv"] >= PMV_COMFORT_LOW) & (df["pmv"] <= PMV_COMFORT_HIGH)
        return in_band.mean() * 100.0

    # Weighted carbon savings: sum(elec_kwh * carbon_intensity) per hour
    # Uses CSV setpoint timing as proxy for when energy was consumed
    bl_carbon = sum(
        _CARBON_BY_HOUR.get(int(row.sim_time) % 24, 400) * abs(row.site_electricity_j)
        for _, row in bl_csv.iterrows()
    )
    ai_carbon = sum(
        _CARBON_BY_HOUR.get(int(row.sim_time) % 24, 400) * abs(row.site_electricity_j)
        for _, row in ai_csv.iterrows()
    )

    return {
        "bl_total_kwh":    bl_total_kwh,
        "ai_total_kwh":    ai_total_kwh,
        "pct_reduction":   pct_reduction,
        "bl_comfort_pct":  comfort_pct(bl_csv),
        "ai_comfort_pct":  comfort_pct(ai_csv),
        "bl_carbon":       bl_carbon,
        "ai_carbon":       ai_carbon,
    }


def print_headline(m: dict):
    direction = "REDUCTION" if m["pct_reduction"] >= 0 else "INCREASE"
    sign = "-" if m["pct_reduction"] >= 0 else "+"
    print("=" * 58)
    print("  ECO-LOOP  ·  AI vs Baseline  ·  Phase 4 Results")
    print("=" * 58)
    print(f"  Electricity {direction}:  {sign}{abs(m['pct_reduction']):.1f}%")
    print(f"  Baseline total:     {m['bl_total_kwh']:.1f} kWh")
    print(f"  AI total:           {m['ai_total_kwh']:.1f} kWh")
    print(f"  Savings:            {m['bl_total_kwh'] - m['ai_total_kwh']:.1f} kWh")
    print()
    print(f"  PMV in-band — baseline: {m['bl_comfort_pct']:.0f}%")
    print(f"  PMV in-band — AI:       {m['ai_comfort_pct']:.0f}%")
    print("=" * 58)


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------

def make_figure(bl_csv, ai_csv, bl_elec, ai_elec, metrics, save_path=None):
    C_BL   = "#6c8ebf"
    C_AI   = "#82b366"
    C_BG   = "#1e1e2e"
    C_AX   = "#2a2a3e"
    C_TXT  = "#cdd6f4"
    C_GRID = "#313244"
    C_BAND = "#a6e3a1"
    C_CARB = "#f9e2af"

    matplotlib.rcParams.update({
        "figure.facecolor":  C_BG,
        "axes.facecolor":    C_AX,
        "axes.edgecolor":    C_GRID,
        "axes.labelcolor":   C_TXT,
        "xtick.color":       C_TXT,
        "ytick.color":       C_TXT,
        "text.color":        C_TXT,
        "grid.color":        C_GRID,
        "grid.linewidth":    0.5,
        "legend.facecolor":  C_AX,
        "legend.edgecolor":  C_GRID,
        "font.family":       "sans-serif",
        "font.size":         9,
    })

    fig = plt.figure(figsize=(15, 9), facecolor=C_BG)
    gs = gridspec.GridSpec(
        2, 3, figure=fig,
        width_ratios=[2.5, 2.5, 1],
        hspace=0.45, wspace=0.35,
    )
    ax_elec  = fig.add_subplot(gs[0, 0])
    ax_temp  = fig.add_subplot(gs[1, 0])
    ax_setpt = fig.add_subplot(gs[0, 1])
    ax_carb  = fig.add_subplot(gs[1, 1])
    ax_kpi   = fig.add_subplot(gs[:, 2])

    # ── 1. Hourly electricity from SQL ───────────────────────────────────
    if not bl_elec.empty and not ai_elec.empty:
        ax_elec.bar(
            bl_elec["hour_of_day"] - 0.2, bl_elec["electricity_kwh"],
            width=0.38, color=C_BL, alpha=0.85, label="Baseline",
        )
        ax_elec.bar(
            ai_elec["hour_of_day"] + 0.2, ai_elec["electricity_kwh"],
            width=0.38, color=C_AI, alpha=0.85, label="AI (LLM)",
        )
    ax_elec.set_title("Electricity by Hour of Day", fontsize=10, fontweight="bold",
                       color=C_TXT, pad=5)
    ax_elec.set_ylabel("kWh", color=C_TXT)
    ax_elec.set_xlabel("Hour of day", color=C_TXT)
    ax_elec.legend(fontsize=8, framealpha=0.6)
    ax_elec.grid(axis="y", alpha=0.4)
    ax_elec.set_xlim(-0.5, 23.5)

    # ── 2. Zone temperature over sim_time ────────────────────────────────
    ax_temp.plot(bl_csv["sim_time"], bl_csv["zone_temp_c"],
                 color=C_BL, lw=1.1, alpha=0.85, label="Baseline zone temp")
    ax_temp.plot(ai_csv["sim_time"], ai_csv["zone_temp_c"],
                 color=C_AI, lw=1.1, alpha=0.85, label="AI zone temp")
    ax_temp.axhspan(config.MIN_COOLING_SETPOINT, config.MAX_COOLING_SETPOINT,
                    alpha=0.07, color=C_BAND, label="Comfort band")
    ax_temp.set_title("Zone Temperature", fontsize=10, fontweight="bold",
                       color=C_TXT, pad=5)
    ax_temp.set_ylabel("°C", color=C_TXT)
    ax_temp.set_xlabel("Simulation time (hours)", color=C_TXT)
    ax_temp.legend(fontsize=8, framealpha=0.6)
    ax_temp.grid(axis="y", alpha=0.4)
    ax_temp.set_xlim(left=0)

    # ── 3. Cooling setpoints over sim_time ───────────────────────────────
    ax_setpt.step(bl_csv["sim_time"], bl_csv["cooling_setpoint_c"],
                  color=C_BL, lw=1.2, alpha=0.85, label="Baseline SP", where="post")
    ax_setpt.step(ai_csv["sim_time"], ai_csv["cooling_setpoint_c"],
                  color=C_AI, lw=1.2, alpha=0.85, label="AI SP", where="post")
    ax_setpt.axhline(config.MIN_COOLING_SETPOINT, color=C_BAND, lw=0.7,
                     linestyle=":", alpha=0.7, label="Band limits")
    ax_setpt.axhline(config.MAX_COOLING_SETPOINT, color=C_BAND, lw=0.7,
                     linestyle=":", alpha=0.7)
    ax_setpt.set_title("Cooling Setpoints", fontsize=10, fontweight="bold",
                        color=C_TXT, pad=5)
    ax_setpt.set_ylabel("°C", color=C_TXT)
    ax_setpt.set_xlabel("Simulation time (hours)", color=C_TXT)
    ax_setpt.legend(fontsize=8, framealpha=0.6)
    ax_setpt.grid(axis="y", alpha=0.4)
    ax_setpt.set_xlim(left=0)
    ax_setpt.set_ylim(
        config.MIN_COOLING_SETPOINT - 0.5,
        config.MAX_COOLING_SETPOINT + 0.5,
    )

    # ── 4. Carbon intensity profile ──────────────────────────────────────
    hours  = list(range(24))
    carbon = [_CARBON_BY_HOUR[h] for h in hours]
    ax_carb.fill_between(hours, carbon, alpha=0.3, color=C_CARB)
    ax_carb.plot(hours, carbon, color=C_CARB, lw=1.3)
    ax_carb.set_title("Grid Carbon Intensity", fontsize=10, fontweight="bold",
                       color=C_TXT, pad=5)
    ax_carb.set_ylabel("gCO₂/kWh", color=C_TXT)
    ax_carb.set_xlabel("Hour of day", color=C_TXT)
    ax_carb.set_xlim(0, 23)
    ax_carb.grid(axis="y", alpha=0.4)
    ax_carb.text(18, 525, "Peak carbon\n(evening)", color=C_CARB,
                 fontsize=7, ha="center", alpha=0.8)

    # ── 5. KPI panel ─────────────────────────────────────────────────────
    ax_kpi.axis("off")
    ax_kpi.set_facecolor(C_AX)

    pct   = metrics["pct_reduction"]
    arrow = "↓" if pct >= 0 else "↑"
    kpi_c = C_AI if pct >= 0 else "#f38ba8"
    savings_kwh = metrics["bl_total_kwh"] - metrics["ai_total_kwh"]

    entries = [
        ("ECO-LOOP", C_TXT, 15, "bold", 0.04),
        ("Building Agent · Phase 4", "#a6adc8", 8, "normal", 0.04),
        ("", C_TXT, 4, "normal", 0.02),
        ("─" * 22, C_GRID, 8, "normal", 0.03),
        ("Energy Savings", "#a6adc8", 8, "normal", 0.04),
        (f"{arrow} {abs(pct):.1f}%", kpi_c, 26, "bold", 0.07),
        (f"{savings_kwh:+.1f} kWh", kpi_c, 11, "normal", 0.04),
        ("", C_TXT, 4, "normal", 0.02),
        ("─" * 22, C_GRID, 8, "normal", 0.03),
        ("PMV Comfort In-Band", "#a6adc8", 8, "normal", 0.04),
        (f"Baseline  {metrics['bl_comfort_pct']:.0f}%", C_BL, 10, "normal", 0.035),
        (f"AI           {metrics['ai_comfort_pct']:.0f}%", C_AI, 10, "normal", 0.04),
        ("", C_TXT, 4, "normal", 0.02),
        ("─" * 22, C_GRID, 8, "normal", 0.03),
        ("Setpoint Band", "#a6adc8", 8, "normal", 0.04),
        (f"{config.MIN_COOLING_SETPOINT}–{config.MAX_COOLING_SETPOINT} °C cooling", C_TXT, 9, "normal", 0.035),
        (f"{config.MIN_HEATING_SETPOINT}–{config.MAX_HEATING_SETPOINT} °C heating", C_TXT, 9, "normal", 0.04),
        ("", C_TXT, 4, "normal", 0.02),
        ("─" * 22, C_GRID, 8, "normal", 0.03),
        ("LLM", "#a6adc8", 8, "normal", 0.04),
        (config.LLM_MODEL, C_TXT, 9, "normal", 0.035),
        (f"via {config.LLM_PROVIDER}", "#a6adc8", 7, "normal", 0.035),
    ]

    y = 0.97
    for text, color, size, weight, dy in entries:
        ax_kpi.text(
            0.5, y, text, transform=ax_kpi.transAxes,
            ha="center", va="top", color=color,
            fontsize=size, fontweight=weight,
        )
        y -= dy

    fig.suptitle(
        "Eco-Loop  ·  AI Building Agent  ·  Energy & Comfort Dashboard",
        fontsize=12, fontweight="bold", color=C_TXT, y=1.005,
    )

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        fig.savefig(save_path, dpi=150, bbox_inches="tight", facecolor=C_BG)
        print(f"Dashboard saved → {save_path}")
    else:
        plt.tight_layout()
        plt.show()

    plt.close(fig)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Eco-Loop Phase 4 dashboard")
    parser.add_argument("--save", action="store_true",
                        help="Save to logs/dashboard.png instead of displaying")
    args = parser.parse_args()

    bl_csv, ai_csv = load_csvs()
    bl_elec, ai_elec, bl_total_kwh, ai_total_kwh = load_sql_electricity()
    metrics = compute_metrics(bl_csv, ai_csv, bl_total_kwh, ai_total_kwh)
    print_headline(metrics)

    save_path = os.path.join(config.LOGS_DIR, "dashboard.png") if args.save else None
    make_figure(bl_csv, ai_csv, bl_elec, ai_elec, metrics, save_path=save_path)


if __name__ == "__main__":
    main()

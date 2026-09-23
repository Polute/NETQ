#!/usr/bin/env python3
"""
aegso_analysis.py – Analysis & Visualization for AESO 3-Node Protocol

Expected CSV (per client):
  round, gen_attempts, t_gen_total_ns, gen_rtt_ns, ts_gen_success_ns,
  ts_swap_ns, swap_to_recv_ns, t_exp_ns, werner,
  pgen_success_bit, pswap_success_bit

Directory layout:
  aegso_report_pswapX_Y/
    └── pgenA_B/
        ├── client_lab_N.csv
        ├── client_teleco_N.csv
        └── repeater_swap_N.csv

Outputs (in 'analysis_AEGSO/'):
  1. E_N vs Tcoh (Scatter points only, bounded error bars [0, 1])
  2. t_swap vs pswap (Scatter points only, non-negative error bars)
  2b. gen_rtt vs pswap (Scatter points only, non-negative error bars)
  5. 60s-Normalized Rate Heatmap (pgen × pswap, YlOrRd colormap)
  6. gen_rtt histogram
  7. pswap success rate bar chart
  8. Swap Coincidences & Successes Heatmap (pgen × pswap, YlOrRd colormap)
  9. Memory Exposure Density Heatmap (t_exp vs E_N binned density + theoretical decay)
  10. Mean Memory Time Heatmap (pgen × pswap, YlOrRd colormap)
  11. Summary table → stdout + .txt
"""

import argparse
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_DIR = "analysis_AEGSO"
SIMULATION_DURATION_S = 60.0  # Normalized duration per simulation run

TCOH_VALUES_NS = [
    np.inf,
    60.0e9,    # 1 min
    1.0e9,     # 1 s
    500.0e6,   # 500 ms
    1.0e6,     # 1 ms
    500.0e3,   # 500 µs
    250.0e3,   # 250 µs
    100.0e3,   # 100 µs
    50.0e3,    # 50 µs
    1.0e3,     # 1 µs
]
TCOH_LABELS = [
    "Inf", "1 min", "1 s", "500 ms", "1 ms",
    "500 µs", "250 µs", "100 µs", "50 µs", "1 µs",
]

# ─────────────────────────────────────────────────────────────────────────────
# Physics & Math Helpers
# ─────────────────────────────────────────────────────────────────────────────


def compute_en(w: np.ndarray) -> np.ndarray:
    """Log-Negativity E_N [e-bits] from Werner parameter W (bounded in [0, 1])."""
    w = np.atleast_1d(np.asarray(w, dtype=float))
    en = np.zeros_like(w)
    mask = (w > 1.0 / 3.0) & np.isfinite(w)
    en[mask] = np.log2((1.0 + 3.0 * w[mask]) / 2.0)
    return np.clip(en, 0.0, 1.0)


def compute_w_experimental(t_exp_ns: np.ndarray, t_coh_ns: float, w0: float = 1.0) -> np.ndarray:
    """W = W0 * exp(-t_exp / Tcoh). If Tcoh=inf → W0."""
    t = np.atleast_1d(np.asarray(t_exp_ns, dtype=float))
    if np.isinf(t_coh_ns):
        return np.full_like(t, w0)
    return w0 * np.exp(-t / t_coh_ns)


def asymmetric_yerr_bounded(means: np.ndarray, stds: np.ndarray,
                            lower_bound: float = 0.0,
                            upper_bound: float = 1.0) -> np.ndarray:
    """
    Computes asymmetric yerr [lower_err, upper_err] so error bars stay
    within [lower_bound, upper_bound].
    """
    means = np.asarray(means, dtype=float)
    stds = np.asarray(stds, dtype=float)

    lower_err = np.minimum(stds, np.maximum(0.0, means - lower_bound))
    upper_err = np.minimum(stds, np.maximum(0.0, upper_bound - means))
    return np.array([lower_err, upper_err])


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────


def parse_pswap(name: str) -> float | None:
    m = re.search(r"pswap([0-9]+(?:_[0-9]+)?)", name)
    return float(m.group(1).replace("_", ".")) if m else None


def parse_pgen(name: str) -> float | None:
    m = re.search(r"pgen([0-9]+(?:_[0-9]+)?)", name)
    return float(m.group(1).replace("_", ".")) if m else None


def find_csv(directory: str, prefix: str) -> str | None:
    for f in sorted(os.listdir(directory)):
        if f.startswith(prefix) and f.lower().endswith(".csv"):
            return os.path.join(directory, f)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Data Loading & Processing
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_COLS = {
    "t_gen_total_ns", "gen_rtt_ns", "ts_swap_ns",
    "swap_to_recv_ns", "werner", "pswap_success_bit",
}


def load_client_csv(filepath: str) -> pd.DataFrame:
    """Load and validate a client CSV."""
    df = pd.read_csv(filepath)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        print(f"  [WARN] Missing required columns in {filepath}: {missing}")
        return pd.DataFrame()
    return df


def compute_t_exp(df: pd.DataFrame) -> pd.Series:
    """Compute EPR exposure time (t_exp)."""
    if "t_exp_ns" in df.columns:
        t_exp = df["t_exp_ns"].copy()
    elif "ts_gen_success_ns" in df.columns:
        t_exp = (df["ts_swap_ns"] - df["ts_gen_success_ns"]).clip(lower=0)
    else:
        t_exp = df["swap_to_recv_ns"].copy()
    return t_exp.clip(lower=0)


def compute_t_swap(df: pd.DataFrame) -> pd.Series:
    """Compute time between consecutive successful pswap events."""
    df_ok = df[df["pswap_success_bit"] == 1].sort_values("ts_swap_ns").reset_index(drop=True)
    if len(df_ok) < 2:
        return pd.Series([0.0] * len(df), index=df.index)

    ts_vals = df_ok["ts_swap_ns"].values
    t_swap_vals = np.zeros(len(df_ok))
    t_swap_vals[0] = 0.0
    t_swap_vals[1:] = np.diff(ts_vals)

    full_t_swap = np.zeros(len(df))
    success_mask = (df["pswap_success_bit"] == 1).values
    ok_idx = 0
    for i in range(len(df)):
        if success_mask[i]:
            full_t_swap[i] = t_swap_vals[ok_idx]
            ok_idx += 1
    return pd.Series(full_t_swap, index=df.index)


def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Add computed columns and return cleaned DataFrame."""
    df = df.copy()
    df["t_exp_ns"] = compute_t_exp(df)
    df["t_swap_ns"] = compute_t_swap(df)
    df["t_total_ns"] = df["t_gen_total_ns"] + df["t_exp_ns"]
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Plotting Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _style():
    plt.rcParams.update({
        "font.size": 11,
        "axes.titlesize": 12,
        "axes.labelsize": 11,
        "legend.fontsize": 9,
        "figure.dpi": 100,
    })


def plot_en_vs_tcoh(en_means, en_stds, pswaps, label, w0, outdir):
    """Plot 1: E_N vs T_coh with scatter points only and bounded error bars [0, 1]."""
    path = os.path.join(outdir, f"01_EN_vs_Tcoh_{label}.png")
    _style()
    fig, ax = plt.subplots(figsize=(9, 5.5))
    x = np.arange(len(TCOH_LABELS))

    for j, p in enumerate(pswaps):
        m = en_means[:, j]
        s = en_stds[:, j]
        yerr = asymmetric_yerr_bounded(m, s, lower_bound=0.0, upper_bound=1.0)

        ax.errorbar(x, m, yerr=yerr, fmt="o", linestyle="none", capsize=4,
                    label=f"$p_{{swap}}$={p:.2f}", alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels(TCOH_LABELS)
    ax.set_xlabel("Coherence Time $T_{coh}$")
    ax.set_ylabel("$E_N$ [e-bits]")
    ax.set_ylim(-0.05, 1.05)
    ax.set_title(f"$E_N$ vs $T_{{coh}}$ ({label}, $W_0$={w0:.2f})")
    ax.grid(True, ls="--", alpha=0.5)
    ax.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_time_vs_pswap(values, stds, pswaps, ylabel, title, label, outdir, fname):
    """Plot 2 & 2b: Time metrics vs pswap with scatter points only and non-negative yerr."""
    path = os.path.join(outdir, fname)
    _style()
    fig, ax = plt.subplots(figsize=(8, 5))
    xs = [f"{p:.2f}" for p in pswaps]

    m = np.asarray(values, dtype=float)
    s = np.asarray(stds, dtype=float)
    yerr = asymmetric_yerr_bounded(m, s, lower_bound=0.0, upper_bound=np.inf)

    ax.errorbar(xs, m, yerr=yerr, fmt="s", linestyle="none", color="navy",
                ecolor="crimson", capsize=5, lw=2, label="mean ± σ")
    ax.set_xlabel("$p_{swap}$")
    ax.set_ylabel(ylabel)
    ax.set_ylim(bottom=0)
    ax.set_title(title)
    ax.grid(True, axis="y", ls="--", alpha=0.5)
    ax.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_rate_pgen_pswap_heatmap(df_master: pd.DataFrame, label: str, w0: float, outdir: str, t_coh_ns: float = 1.0e6):
    """
    Plot 5: Heatmap of normalized entanglement rate [e-bits/s] over a 60-second execution window
    across (pgen × pswap) grid using YlOrRd colormap.
    """
    path = os.path.join(outdir, f"05_rate_pgen_pswap_heatmap_{label}.png")
    _style()

    if df_master.empty or "pgen" not in df_master.columns or "pswap" not in df_master.columns:
        return

    df_calc = df_master.copy()
    df_calc["w_exp"] = compute_w_experimental(df_calc["t_exp_ns"].values, t_coh_ns, w0=w0)
    df_calc["en"] = compute_en(df_calc["w_exp"].values)
    df_calc["en_ok"] = np.where(df_calc["pswap_success_bit"] == 1, df_calc["en"], 0.0)

    grouped = df_calc.groupby(["pgen", "pswap"]).agg(
        total_en=("en_ok", "sum")
    ).reset_index()

    grouped["rate_60s"] = grouped["total_en"] / SIMULATION_DURATION_S

    rate_pivot = grouped.pivot(index="pgen", columns="pswap", values="rate_60s")

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.heatmap(
        rate_pivot,
        annot=True,
        fmt=".2f",
        cmap="YlOrRd",
        cbar_kws={"label": r"Normalized Rate $\langle R_{60s} \rangle$ [e-bits/s]"},
        ax=ax,
        linewidths=0.5,
        linecolor="gray"
    )
    ax.set_xlabel("$p_{swap}$")
    ax.set_ylabel("$p_{gen}$")
    ax.set_title(f"Normalized Entanglement Rate over 60s ($p_{{gen}}$ × $p_{{swap}}$) – {label}")
    ax.invert_yaxis()

    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_rtt_hist(rtt_ns_all, label, outdir):
    """Plot 6: Histogram of generation RTT."""
    path = os.path.join(outdir, f"06_gen_rtt_hist_{label}.png")
    if not rtt_ns_all:
        return
    _style()
    fig, ax = plt.subplots(figsize=(8, 5))
    vals = np.asarray(rtt_ns_all) / 1000.0  # µs
    ax.hist(vals, bins=50, color="steelblue", edgecolor="w", alpha=0.85)
    med = np.median(vals)
    p99 = np.percentile(vals, 99)
    ax.axvline(med, color="red", ls="--", label=f"median={med:.1f} µs")
    ax.axvline(p99, color="orange", ls="--", label=f"P99={p99:.1f} µs")
    ax.set_xlabel("$gen\\_rtt$ [µs]")
    ax.set_ylabel("Count")
    ax.set_title(f"Generation RTT distribution ({label})")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_pswap_success_rate(pswap_rates, pswaps, label, outdir):
    """Plot 7: Bar chart of observed pswap success rate."""
    path = os.path.join(outdir, f"07_pswap_success_{label}.png")
    _style()
    fig, ax = plt.subplots(figsize=(8, 4))
    xs = [f"{p:.2f}" for p in pswaps]
    ax.bar(xs, pswap_rates, color="seagreen", alpha=0.85, edgecolor="white")
    for x, r in zip(xs, pswap_rates):
        ax.text(x, r + 0.01, f"{r:.3f}", ha="center", fontsize=9)
    ax.set_ylim(0, 1.1)
    ax.set_xlabel("$p_{swap}$ (set)")
    ax.set_ylabel("Observed pswap success rate")
    ax.set_title(f"PSwap Success Rate ({label})")
    ax.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_pswap_pgen_attempts_success_heatmap(df_master: pd.DataFrame, label: str, outdir: str):
    """
    Plot 8: Heatmap of swap attempts (coincidences) across (pgen × pswap) grid
    colored with YlOrRd, displaying only successful swap counts inside cells.
    """
    path = os.path.join(outdir, f"08_pswap_pgen_attempts_heatmap_{label}.png")
    _style()

    if df_master.empty or "pgen" not in df_master.columns or "pswap" not in df_master.columns:
        return

    grouped = df_master.groupby(["pgen", "pswap"]).agg(
        attempts=("pswap_success_bit", "count"),
        successes=("pswap_success_bit", lambda x: int((x == 1).sum()))
    ).reset_index()

    if grouped.empty:
        return

    attempts_pivot = grouped.pivot(index="pgen", columns="pswap", values="attempts")
    success_pivot = grouped.pivot(index="pgen", columns="pswap", values="successes")

    annot_matrix = np.empty(success_pivot.shape, dtype=object)
    for i in range(success_pivot.shape[0]):
        for j in range(success_pivot.shape[1]):
            val = success_pivot.iloc[i, j]
            annot_matrix[i, j] = f"{int(val)}" if not np.isnan(val) else "0"

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.heatmap(
        attempts_pivot,
        annot=annot_matrix,
        fmt="",
        cmap="YlOrRd",
        cbar_kws={"label": "Swap Attempts (Coincidences)"},
        ax=ax,
        linewidths=0.5,
        linecolor="gray"
    )
    ax.set_xlabel("$p_{swap}$")
    ax.set_ylabel("$p_{gen}$")
    ax.set_title(f"Swap Attempts & Successful Swaps ($p_{{gen}}$ × $p_{{swap}}$) – {label}")
    ax.invert_yaxis()

    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_texp_en_density_heatmap(df_master: pd.DataFrame, label: str, w0: float, outdir: str, t_coh_ns: float = 1.0e6):
    """
    Plot 9: 2D Binned Density Heatmap of Memory Exposure Time (t_exp) vs Log-Negativity (E_N).
    Replaces overlapping scatter points with a clear 2D histogram density grid (YlOrRd)
    and superimposes the theoretical decay curve.
    """
    path = os.path.join(outdir, f"09_texp_vs_EN_density_heatmap_{label}.png")
    _style()

    df_ok = df_master[df_master["pswap_success_bit"] == 1].copy()
    if df_ok.empty:
        return

    texp_us = df_ok["t_exp_ns"].values / 1e3
    w_exp = compute_w_experimental(df_ok["t_exp_ns"].values, t_coh_ns, w0=w0)
    en_exp = compute_en(w_exp)

    fig, ax = plt.subplots(figsize=(9, 5.5))

    # 2D Binned Histogram / Density Heatmap
    x_bins = np.linspace(0, max(np.percentile(texp_us, 99.5), 10.0), 30)
    y_bins = np.linspace(0.0, 1.0, 20)

    counts, x_edges, y_edges = np.histogram2d(texp_us, en_exp, bins=[x_bins, y_bins])

    # Plot 2D Mesh with YlOrRd colormap
    mesh = ax.pcolormesh(
        x_edges, y_edges, counts.T,
        cmap="YlOrRd", shading="flat", edgecolors="none"
    )
    cbar = fig.colorbar(mesh, ax=ax)
    cbar.set_label("Número de pares (Frecuencia)")

    # Overlay theoretical decay curve
    t_curve_us = np.linspace(0, x_edges[-1], 300)
    w_curve = compute_w_experimental(t_curve_us * 1e3, t_coh_ns, w0=w0)
    en_curve = compute_en(w_curve)

    ax.plot(
        t_curve_us, en_curve, color="darkblue", linestyle="--", lw=2,
        label=f"Decaimiento Teórico ($T_{{coh}}$ = {t_coh_ns/1e6:.1f} ms)"
    )

    ax.set_xlabel("Tiempo en Memoria $t_{exp}$ [µs]")
    ax.set_ylabel("Entrelazamiento $E_N$ [e-bits]")
    ax.set_ylim(-0.02, 1.02)
    ax.set_title(f"Mapa de Densidad: Degradación de $E_N$ por $t_{{exp}}$ – {label}")
    ax.grid(True, ls="--", alpha=0.3, color="gray")
    ax.legend(loc="upper right")

    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_texp_pgen_pswap_heatmap(df_master: pd.DataFrame, label: str, outdir: str):
    """
    Plot 10: Heatmap of Mean Memory Exposure Time <t_exp> in µs across (pgen × pswap) grid
    using YlOrRd colormap.
    """
    path = os.path.join(outdir, f"10_mean_texp_pgen_pswap_heatmap_{label}.png")
    _style()

    if df_master.empty or "pgen" not in df_master.columns or "pswap" not in df_master.columns:
        return

    df_ok = df_master[df_master["pswap_success_bit"] == 1].copy()
    if df_ok.empty:
        return

    df_ok["t_exp_us"] = df_ok["t_exp_ns"] / 1e3

    grouped = df_ok.groupby(["pgen", "pswap"]).agg(
        mean_texp=("t_exp_us", "mean")
    ).reset_index()

    texp_pivot = grouped.pivot(index="pgen", columns="pswap", values="mean_texp")

    fig, ax = plt.subplots(figsize=(10, 6))
    sns.heatmap(
        texp_pivot,
        annot=True,
        fmt=".1f",
        cmap="YlOrRd",
        cbar_kws={"label": r"Tiempo medio en memoria $\langle t_{exp} \rangle$ [µs]"},
        ax=ax,
        linewidths=0.5,
        linecolor="gray"
    )
    ax.set_xlabel("$p_{swap}$")
    ax.set_ylabel("$p_{gen}$")
    ax.set_title(f"Tiempo Medio de Espera en Memoria ($p_{{gen}}$ × $p_{{swap}}$) – {label}")
    ax.invert_yaxis()

    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────


def print_summary(pswap_data: dict, sorted_pswaps: list, label: str,
                  w0: float, outdir: str):
    div = "=" * 114
    title = f"AEGSO METRICS – {label}  (W0={w0:.2f})"

    headers = [
        "pswap", "M_ok", "pgen_Navg", "t_gen_µs", "t_exp_µs",
        "t_total_µs", "gen_rtt_µs", "W_repeater", "EN_mean", "Rate_ebits/s",
    ]

    rows = []
    for p in sorted_pswaps:
        df = pswap_data[p]
        df_ok = df[df["pswap_success_bit"] == 1]
        if df_ok.empty:
            continue
        M = len(df_ok)
        n_avg = df["gen_attempts"].mean() if "gen_attempts" in df.columns else 0
        t_gen_us = df_ok["t_gen_total_ns"].mean() / 1e3
        t_exp_us = df_ok["t_exp_ns"].mean() / 1e3
        t_tot_us = df_ok["t_total_ns"].mean() / 1e3
        rtt_us = df_ok["gen_rtt_ns"].mean() / 1e3
        w_rep = df_ok["werner"].mean()
        en = compute_en(compute_w_experimental(
            df_ok["t_exp_ns"].values, 1.0e6, w0=w0))
        en_mean = en.mean()
        rate = en.sum() / SIMULATION_DURATION_S

        rows.append([
            f"{p:.2f}", f"{M:d}", f"{n_avg:.2f}", f"{t_gen_us:.2f}",
            f"{t_exp_us:.2f}", f"{t_tot_us:.2f}", f"{rtt_us:.2f}",
            f"{w_rep:.4f}", f"{en_mean:.6f}", f"{rate:.2f}",
        ])

    if not rows:
        print(f"  [!] No successful pswap data for {label}")
        return

    widths = [max(len(h), max(len(r[i]) for r in rows)) + 2 for i, h in enumerate(headers)]
    lines = [div, title.center(len(div)), div]
    lines.append("".join(f"{h:^{widths[i]}}" for i, h in enumerate(headers)))
    lines.append("-" * len(div))
    for r in rows:
        lines.append("".join(f"{v:^{widths[i]}}" for i, v in enumerate(r)))
    lines.append(div)

    table = "\n".join(lines)
    print(f"\n{table}\n")

    fpath = os.path.join(outdir, f"summary_{label}.txt")
    with open(fpath, "w") as f:
        f.write(table + "\n")
    print(f"  [+] {fpath}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="AEGSO Entanglement Analysis")
    parser.add_argument("--data-dir", default=".",
                        help="Base dir with aegso_report_pswap* folders")
    parser.add_argument("--percentile", type=float, default=99.0,
                        help="Upper P% filter on t_total_ns (100 = no filter)")
    parser.add_argument("--w1", type=float, default=1.0,
                        help="W0 for Client Lab")
    parser.add_argument("--w2", type=float, default=1.0,
                        help="W0 for Client Teleco")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base = os.path.abspath(args.data_dir)

    pswap_dirs = sorted([
        os.path.join(base, d) for d in os.listdir(base)
        if d.startswith("aegso_report_pswap")
        and os.path.isdir(os.path.join(base, d))
    ])
    if not pswap_dirs:
        print(f"[!] No aegso_report_pswap* folders in {base}")
        return

    print(f"[*] Found {len(pswap_dirs)} pswap folder(s):")
    for d in pswap_dirs:
        print(f"    {os.path.basename(d)} → pswap={parse_pswap(os.path.basename(d))}")

    client_modes = [
        ("lab",    "Client Lab",    "client_lab",    args.w1),
        ("teleco", "Client Teleco", "client_teleco", args.w2),
    ]

    for mode_key, mode_label, csv_prefix, w0 in client_modes:
        print(f"\n{'='*64}")
        print(f"[*] {mode_label}  (W0={w0:.2f})")
        print(f"{'='*64}")

        all_dfs = []

        for pswap_dir in pswap_dirs:
            pswap = parse_pswap(os.path.basename(pswap_dir))
            if pswap is None:
                continue

            pgen_dirs = sorted([
                os.path.join(pswap_dir, d) for d in os.listdir(pswap_dir)
                if d.startswith("pgen") and os.path.isdir(os.path.join(pswap_dir, d))
            ])
            search_dirs = pgen_dirs if pgen_dirs else [pswap_dir]

            for sd in search_dirs:
                pgen = parse_pgen(os.path.basename(sd))
                fp = find_csv(sd, csv_prefix)
                if fp is None or os.path.getsize(fp) == 0:
                    continue
                df_raw = load_client_csv(fp)
                if df_raw.empty:
                    continue
                df_raw = prepare_dataframe(df_raw)
                df_raw["pswap"] = pswap
                df_raw["pgen"] = pgen if pgen is not None else 1.0

                if args.percentile < 100.0 and len(df_raw) > 1:
                    cutoff = np.percentile(df_raw["t_total_ns"].values, args.percentile)
                    df_raw = df_raw[df_raw["t_total_ns"] <= cutoff].reset_index(drop=True)

                all_dfs.append(df_raw)

        if not all_dfs:
            print(f"  [!] No data for {mode_label}")
            continue

        df_master = pd.concat(all_dfs, ignore_index=True)

        pswap_data = {
            p: df_master[df_master["pswap"] == p].reset_index(drop=True)
            for p in sorted(df_master["pswap"].unique())
        }
        sorted_pswaps = sorted(pswap_data.keys())

        # ── Compute Matrices ──
        n_tcoh = len(TCOH_VALUES_NS)
        n_pswap = len(sorted_pswaps)

        en_means_mat = np.zeros((n_tcoh, n_pswap))
        en_stds_mat  = np.zeros((n_tcoh, n_pswap))

        tswap_vals, tswap_stds = [], []
        rtt_vals, rtt_stds = [], []
        pswap_rates = []
        all_rtt = []

        for j, p in enumerate(sorted_pswaps):
            df = pswap_data[p]
            df_ok = df[df["pswap_success_bit"] == 1].copy()

            pswap_rates.append(
                (df["pswap_success_bit"] == 1).mean() if len(df) > 0 else 0.0
            )

            if df_ok.empty:
                continue

            rtt = df_ok["gen_rtt_ns"].values / 1e3  # µs
            all_rtt.extend(df_ok["gen_rtt_ns"].values)

            rtt_vals.append(rtt.mean())
            rtt_stds.append(rtt.std())

            df_sorted = df_ok.sort_values("ts_swap_ns").reset_index(drop=True)
            if len(df_sorted) >= 2:
                ts_arr = df_sorted["ts_swap_ns"].values
                t_swap_s = np.diff(ts_arr) / 1e9
                tswap_vals.append(t_swap_s.mean() * 1e3)  # ms
                tswap_stds.append(t_swap_s.std() * 1e3)
            else:
                tswap_vals.append(0.0)
                tswap_stds.append(0.0)

            t_exp_ns = df_ok["t_exp_ns"].values
            for i, t_coh in enumerate(TCOH_VALUES_NS):
                w_exp = compute_w_experimental(t_exp_ns, t_coh, w0=w0)
                en_exp = compute_en(w_exp)
                en_means_mat[i, j] = en_exp.mean()
                en_stds_mat[i, j]  = en_exp.std()

        # ── Plots ──
        print(f"\n  [*] Generating plots...")
        plot_en_vs_tcoh(en_means_mat, en_stds_mat, sorted_pswaps, mode_key, w0, OUTPUT_DIR)
        plot_time_vs_pswap(
            tswap_vals, tswap_stds, sorted_pswaps,
            "$t_{swap}$ [ms]", f"Swap Interval vs $p_{{swap}}$ ({mode_label})",
            mode_key, OUTPUT_DIR, f"02_tswap_vs_pswap_{mode_key}.png"
        )
        plot_time_vs_pswap(
            rtt_vals, rtt_stds, sorted_pswaps,
            "$gen\\_rtt$ [µs]", f"Generation RTT vs $p_{{swap}}$ ({mode_label})",
            mode_key, OUTPUT_DIR, f"02b_gen_rtt_vs_pswap_{mode_key}.png"
        )
        plot_rate_pgen_pswap_heatmap(df_master, mode_key, w0, OUTPUT_DIR)
        plot_rtt_hist(all_rtt, mode_key, OUTPUT_DIR)
        plot_pswap_success_rate(pswap_rates, sorted_pswaps, mode_key, OUTPUT_DIR)
        plot_pswap_pgen_attempts_success_heatmap(df_master, mode_key, OUTPUT_DIR)
        plot_texp_en_density_heatmap(df_master, mode_key, w0, OUTPUT_DIR, t_coh_ns=1.0e6)
        plot_texp_pgen_pswap_heatmap(df_master, mode_key, OUTPUT_DIR)

        # ── Summary ──
        print_summary(pswap_data, sorted_pswaps, mode_key, w0, OUTPUT_DIR)

        print(f"\n  [*] Filter P{args.percentile:.0f} | {len(sorted_pswaps)} pswap | {mode_label}")

    print(f"\n[*] All outputs saved to → {os.path.abspath(OUTPUT_DIR)}/")


if __name__ == "__main__":
    main()
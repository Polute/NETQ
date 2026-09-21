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

Metrics:
  • t_gen  = total generation time (t_gen_total_ns)
  • t_exp  = EPR exposure time (t_exp_ns = ts_swap - ts_gen_success)
  • W(t)   = W0 * exp(-t_exp / Tcoh)
  • E_N(W) = log2((1+3W)/2) if W > 1/3 else 0
  • Rate   = E_N / (t_gen + t_exp)  [per successful swap]

Outputs (in 'analysis_AEGSO/'):
  1. E_N vs Tcoh (Mean ± Std) per pswap
  2. t_swap vs pswap
  3. t_gen vs pswap
  4. t_exp vs pswap
  5. Rate heatmap (pswap × Tcoh)
  6. Raw E_N heatmap (using repeater werner)
  7. gen_rtt histogram
  8. Summary table → stdout + .txt
  9. Rate matrix → .csv
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
# Physics
# ─────────────────────────────────────────────────────────────────────────────


def compute_en(w: np.ndarray) -> np.ndarray:
    """Log-Negativity E_N [e-bits] from Werner parameter W."""
    w = np.atleast_1d(np.asarray(w, dtype=float))
    en = np.zeros_like(w)
    mask = (w > 1.0 / 3.0) & np.isfinite(w)
    en[mask] = np.log2((1.0 + 3.0 * w[mask]) / 2.0)
    return en


def compute_w_experimental(t_exp_ns: np.ndarray, t_coh_ns: float, w0: float = 1.0) -> np.ndarray:
    """W = W0 * exp(-t_exp / Tcoh). If Tcoh=inf → W0."""
    t = np.atleast_1d(np.asarray(t_exp_ns, dtype=float))
    if np.isinf(t_coh_ns):
        return np.full_like(t, w0)
    return w0 * np.exp(-t / t_coh_ns)


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
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_COLS = {
    "t_gen_total_ns", "gen_rtt_ns", "ts_swap_ns",
    "swap_to_recv_ns", "werner", "pswap_success_bit",
}

OPTIONAL_COLS = {"t_exp_ns", "ts_gen_success_ns", "pgen_success_bit", "gen_attempts"}


def load_client_csv(filepath: str) -> pd.DataFrame:
    """Load and validate a client CSV."""
    df = pd.read_csv(filepath)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        print(f"  [WARN] Missing required columns in {filepath}: {missing}")
        return pd.DataFrame()
    return df


def compute_t_exp(df: pd.DataFrame) -> pd.Series:
    """
    Compute t_exp (EPR exposure time).
    Priority:
      1. If 't_exp_ns' column exists → use it directly.
      2. If 'ts_gen_success_ns' exists → ts_swap_ns - ts_gen_success_ns.
      3. Fallback: swap_to_recv_ns (approximation).
    """
    if "t_exp_ns" in df.columns:
        t_exp = df["t_exp_ns"].copy()
    elif "ts_gen_success_ns" in df.columns:
        t_exp = (df["ts_swap_ns"] - df["ts_gen_success_ns"]).clip(lower=0)
    else:
        t_exp = df["swap_to_recv_ns"].copy()
    return t_exp.clip(lower=0)


def compute_t_swap(df: pd.DataFrame) -> pd.Series:
    """
    t_swap = time between consecutive SUCCESSFUL pswap events.
    (Only meaningful when pswap_success_bit == 1)
    """
    df_ok = df[df["pswap_success_bit"] == 1].sort_values("ts_swap_ns").reset_index(drop=True)
    if len(df_ok) < 2:
        return pd.Series([0.0] * len(df), index=df.index)

    # Map back: for each successful row, t_swap = ts_swap[i] - ts_swap[i-1]
    ts_vals = df_ok["ts_swap_ns"].values
    t_swap_vals = np.zeros(len(df_ok))
    t_swap_vals[0] = 0.0  # first success has no predecessor
    t_swap_vals[1:] = np.diff(ts_vals)

    # Rebuild full-length series (0 for non-success rows)
    full_t_swap = np.zeros(len(df))
    success_mask = (df["pswap_success_bit"] == 1).values
    # Assign in order
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
# Plotting helpers
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
    path = os.path.join(outdir, f"01_EN_vs_Tcoh_{label}.png")
    _style()
    fig, ax = plt.subplots(figsize=(9, 5.5))
    x = np.arange(len(TCOH_LABELS))
    for j, p in enumerate(pswaps):
        ax.errorbar(x, en_means[:, j], yerr=en_stds[:, j],
                    marker="o", capsize=4, label=f"$p_{{swap}}$={p:.2f}",
                    alpha=0.85, lw=1.5)
    ax.set_xticks(x, TCOH_LABELS)
    ax.set_xlabel("Coherence Time $T_{coh}$")
    ax.set_ylabel("$E_N$ [e-bits]")
    ax.set_title(f"$E_N$ vs $T_{{coh}}$ ({label}, $W_0$={w0:.2f})")
    ax.grid(True, ls="--", alpha=0.5)
    ax.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_time_vs_pswap(values, stds, pswaps, ylabel, title, label, outdir, fname):
    path = os.path.join(outdir, fname)
    _style()
    fig, ax = plt.subplots(figsize=(8, 5))
    xs = [f"{p:.2f}" for p in pswaps]
    ax.errorbar(xs, values, yerr=stds, marker="s", color="navy",
                ecolor="crimson", capsize=5, lw=2, label="mean ± σ")
    ax.set_xlabel("$p_{swap}$")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, axis="y", ls="--", alpha=0.5)
    ax.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_dual_time_vs_pswap(tgen_vals, tgen_stds, texp_vals, texp_stds,
                            pswaps, label, outdir):
    """Plot t_gen and t_exp on the same figure (dual axis)."""
    path = os.path.join(outdir, f"03_tgen_texp_vs_pswap_{label}.png")
    _style()
    fig, ax1 = plt.subplots(figsize=(8, 5))
    xs = [f"{p:.2f}" for p in pswaps]
    xoff = 0.18

    ax1.bar([x + f"-{xoff}" for x in xs], tgen_vals, width=0.36,
            color="steelblue", alpha=0.8, label="$t_{gen}$ (mean)")
    ax1.errorbar([x + f"-{xoff}" for x in xs], tgen_vals, yerr=tgen_stds,
                 fmt="none", ecolor="steelblue", capsize=4, lw=1.5)
    ax1.set_xlabel("$p_{swap}$")
    ax1.set_ylabel("$t_{gen}$ [µs]", color="steelblue")
    ax1.tick_params(axis="y", labelcolor="steelblue")

    ax2 = ax1.twinx()
    ax2.bar([x + f"+{xoff}" for x in xs], texp_vals, width=0.36,
            color="darkorange", alpha=0.8, label="$t_{exp}$ (mean)")
    ax2.errorbar([x + f"+{xoff}" for x in xs], texp_vals, yerr=texp_stds,
                 fmt="none", ecolor="darkorange", capsize=4, lw=1.5)
    ax2.set_ylabel("$t_{exp}$ [µs]", color="darkorange")
    ax2.tick_params(axis="y", labelcolor="darkorange")

    ax1.set_title(f"$t_{{gen}}$ & $t_{{exp}}$ vs $p_{{swap}}$ ({label})")
    ax1.grid(True, axis="y", alpha=0.3)
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left")
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_rate_heatmap(rate_mat, pswaps, label, w0, outdir):
    path = os.path.join(outdir, f"04_heatmap_Rate_{label}.png")
    _style()
    fig, ax = plt.subplots(figsize=(10, 6))
    xs = [f"{p:.2f}" for p in pswaps]
    sns.heatmap(rate_mat, annot=True, fmt=".2f", cmap="YlOrRd",
                xticklabels=xs, yticklabels=TCOH_LABELS,
                cbar_kws={"label": r"$\langle R \rangle$ [e-bits/s]"}, ax=ax)
    ax.set_xlabel("$p_{swap}$")
    ax.set_ylabel("$T_{coh}$")
    ax.set_title(
        r"Rate $\langle R \rangle = E_N / (t_{gen}+t_{exp})$"
        f"\n({label}, $W_0$={w0:.2f})"
    )
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_raw_en(en_mat, pswaps, label, outdir):
    path = os.path.join(outdir, f"05_raw_EN_{label}.png")
    _style()
    fig, ax = plt.subplots(figsize=(9, 3))
    xs = [f"{p:.2f}" for p in pswaps]
    sns.heatmap(en_mat, annot=True, fmt=".4f", cmap="YlGnBu",
                xticklabels=xs, yticklabels=["Repeater $W$"],
                cbar_kws={"label": "$E_N$ [e-bits]"}, ax=ax)
    ax.set_xlabel("$p_{swap}$")
    ax.set_title(f"Actual $E_N$ (repeater Werner) – {label}")
    plt.tight_layout()
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [+] {path}")


def plot_rtt_hist(rtt_ns_all, label, outdir):
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
    """Bar chart of pswap success rate per pswap value."""
    path = os.path.join(outdir, f"07_pswap_success_{label}.png")
    _style()
    fig, ax = plt.subplots(figsize=(8, 4))
    xs = [f"{p:.2f}" for p in pswaps]
    ax.bar(xs, pswap_rates, color="seagreen", alpha=0.85, edgecolor="white")
    for i, (x, r) in enumerate(zip(xs, pswap_rates)):
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
        t_total_s = df_ok["t_total_ns"].values / 1e9
        rate = (en / t_total_s).mean() if t_total_s.max() > 0 else 0.0

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

    # Discover pswap folders
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

        pswap_data: dict[float, pd.DataFrame] = {}

        for pswap_dir in pswap_dirs:
            pswap = parse_pswap(os.path.basename(pswap_dir))
            if pswap is None:
                continue

            # Look for pgen subdirs or flat
            pgen_dirs = sorted([
                os.path.join(pswap_dir, d) for d in os.listdir(pswap_dir)
                if d.startswith("pgen") and os.path.isdir(os.path.join(pswap_dir, d))
            ])
            search_dirs = pgen_dirs if pgen_dirs else [pswap_dir]

            all_dfs = []
            for sd in search_dirs:
                fp = find_csv(sd, csv_prefix)
                if fp is None or os.path.getsize(fp) == 0:
                    continue
                df_raw = load_client_csv(fp)
                if df_raw.empty:
                    continue
                df_raw = prepare_dataframe(df_raw)
                all_dfs.append(df_raw)

            if not all_dfs:
                continue

            df_concat = pd.concat(all_dfs, ignore_index=True)

            # Filter: only rows where pswap succeeded (for timing analysis)
            # But keep all rows for success-rate computation
            if args.percentile < 100.0 and len(df_concat) > 1:
                cutoff = np.percentile(df_concat["t_total_ns"].values, args.percentile)
                df_concat = df_concat[df_concat["t_total_ns"] <= cutoff].reset_index(drop=True)

            pswap_data[pswap] = df_concat
            n_ok = int((df_concat["pswap_success_bit"] == 1).sum())
            print(f"  pswap={pswap:.2f}: {len(df_concat)} rows, {n_ok} successful pswap")

        sorted_pswaps = sorted(pswap_data.keys())
        if not sorted_pswaps:
            print(f"  [!] No data for {mode_label}")
            continue

        # ── Compute matrices ──
        n_tcoh = len(TCOH_VALUES_NS)
        n_pswap = len(sorted_pswaps)

        en_means_mat = np.zeros((n_tcoh, n_pswap))
        en_stds_mat  = np.zeros((n_tcoh, n_pswap))
        rate_mat     = np.zeros((n_tcoh, n_pswap))

        tgen_vals, tgen_stds = [], []
        texp_vals, texp_stds = [], []
        tswap_vals, tswap_stds = [], []
        rtt_vals, rtt_stds = [], []
        pswap_rates = []
        all_rtt = []

        for j, p in enumerate(sorted_pswaps):
            df = pswap_data[p]
            df_ok = df[df["pswap_success_bit"] == 1].copy()

            # Success rate
            pswap_rates.append(
                (df["pswap_success_bit"] == 1).mean() if len(df) > 0 else 0.0
            )

            if df_ok.empty:
                continue

            t_gen = df_ok["t_gen_total_ns"].values / 1e3   # µs
            t_exp = df_ok["t_exp_ns"].values / 1e3        # µs
            t_tot = df_ok["t_total_ns"].values / 1e9      # s
            rtt   = df_ok["gen_rtt_ns"].values / 1e3      # µs
            all_rtt.extend(df_ok["gen_rtt_ns"].values)

            tgen_vals.append(t_gen.mean())
            tgen_stds.append(t_gen.std())
            texp_vals.append(t_exp.mean())
            texp_stds.append(t_exp.std())
            rtt_vals.append(rtt.mean())
            rtt_stds.append(rtt.std())

            # t_swap (between consecutive successes)
            df_sorted = df_ok.sort_values("ts_swap_ns").reset_index(drop=True)
            if len(df_sorted) >= 2:
                ts_arr = df_sorted["ts_swap_ns"].values
                t_swap_s = np.diff(ts_arr) / 1e9
                tswap_vals.append(t_swap_s.mean() * 1e3)  # ms
                tswap_stds.append(t_swap_s.std() * 1e3)
            else:
                tswap_vals.append(0.0)
                tswap_stds.append(0.0)

            # Tcoh sweep
            t_exp_ns = df_ok["t_exp_ns"].values  # ns
            for i, t_coh in enumerate(TCOH_VALUES_NS):
                w_exp = compute_w_experimental(t_exp_ns, t_coh, w0=w0)
                en_exp = compute_en(w_exp)
                en_means_mat[i, j] = en_exp.mean()
                en_stds_mat[i, j]  = en_exp.std()
                valid = t_tot > 0
                if valid.any():
                    rate_mat[i, j] = (en_exp[valid] / t_tot[valid]).mean()

        # Actual E_N from repeater werner
        en_actual_mat = np.zeros((1, n_pswap))
        for j, p in enumerate(sorted_pswaps):
            df_ok = pswap_data[p][pswap_data[p]["pswap_success_bit"] == 1]
            if not df_ok.empty:
                en_actual_mat[0, j] = compute_en(df_ok["werner"].values).mean()

        # ── Plots ──
        print(f"\n  [*] Plots...")
        plot_en_vs_tcoh(en_means_mat, en_stds_mat, sorted_pswaps,
                        mode_key, w0, OUTPUT_DIR)
        plot_time_vs_pswap(
            tswap_vals, tswap_stds, sorted_pswaps,
            "$t_{swap}$ [ms]", f"Swap Interval vs $p_{{swap}}$ ({mode_label})",
            mode_key, OUTPUT_DIR, f"02_tswap_vs_pswap_{mode_key}.png"
        )
        plot_dual_time_vs_pswap(
            tgen_vals, tgen_stds, texp_vals, texp_stds,
            sorted_pswaps, mode_label, OUTPUT_DIR
        )
        plot_time_vs_pswap(
            rtt_vals, rtt_stds, sorted_pswaps,
            "$gen\\_rtt$ [µs]", f"Generation RTT vs $p_{{swap}}$ ({mode_label})",
            mode_key, OUTPUT_DIR, f"02b_gen_rtt_vs_pswap_{mode_key}.png"
        )
        plot_rate_heatmap(rate_mat, sorted_pswaps, mode_key, w0, OUTPUT_DIR)
        plot_raw_en(en_actual_mat, sorted_pswaps, mode_key, OUTPUT_DIR)
        plot_rtt_hist(all_rtt, mode_key, OUTPUT_DIR)
        plot_pswap_success_rate(pswap_rates, sorted_pswaps, mode_key, OUTPUT_DIR)

        # ── Summary ──
        print_summary(pswap_data, sorted_pswaps, mode_key, w0, OUTPUT_DIR)

        # ── Export ──
        xs = [f"pswap_{p:.2f}" for p in sorted_pswaps]
        df_rate = pd.DataFrame(rate_mat, index=TCOH_LABELS, columns=xs)
        csv_out = os.path.join(OUTPUT_DIR, f"matrix_rate_{mode_key}.csv")
        df_rate.to_csv(csv_out)
        print(f"  [+] Rate matrix → {csv_out}")

        print(f"\n  [*] Filter P{args.percentile:.0f} | {len(sorted_pswaps)} pswap | {mode_label}")

    print(f"\n[*] All outputs → {os.path.abspath(OUTPUT_DIR)}/")


if __name__ == "__main__":
    main()
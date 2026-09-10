#!/usr/bin/env python3
"""AEGO Entanglement Generation Analysis & Visualization Suite

Processes empirical timing data with percentile-based outlier filtering.
Saves all plots and matrices to the 'analysis_AEGO_2' folder:
1. E_N vs Tcoh (Mean with Error Bars / Standard Deviation).
2. t_gen vs pgen (Mean and Standard Deviation).
3. Rate Heatmap (Eq. 8) as a function of pgen (x-axis) and Tcoh (y-axis).
4. Summary ASCII table printed to stdout terminal and saved as .txt file.

Generates two sets of outputs:
- One-way exposure time: tr_vals_ns = [r['e2r_ns'] for r in current_attempts]
- Full round-trip exposure time: tr_vals_ns = [r['rtt_ns'] for r in current_attempts]
"""

import argparse
import os
import re
import matplotlib
matplotlib.use('Agg')
import matplotlib.colors as mcolors
from matplotlib.colors import LogNorm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

# Output directory for saving plots and results
OUTPUT_DIR = "analysis_AEGO_2"

# Coherence time sweep values (in nanoseconds) and display labels
TCOH_VALUES_NS = [
    np.inf,              # Infinite
    60.0 * 1e9,          # 1 min
    1.0 * 1e9,           # 1 s
    500.0 * 1e6,         # 500 ms
    1.0 * 1e6,           # 1 ms
    500.0 * 1e3,         # 500 us
    1.0 * 1e3            # 1 us
]

TCOH_LABELS = ['Infinite', '1 min', '1 s', '500 ms', '1 ms', '500 us', '1 us']

def compute_w_experimental(t_roundtrip_ns, t_coh_ns, w0=1.00):
    """Calculates Werner state parameter W based on exposure time and Tcoh."""
    if np.isinf(t_coh_ns):
        return np.full_like(t_roundtrip_ns, w0)
    return w0 * np.exp(-t_roundtrip_ns / t_coh_ns)

def compute_log_negativity(w):
    """Calculates Logarithmic Negativity E_N(W)."""
    w_arr = np.atleast_1d(w).astype(float)
    en = np.zeros_like(w_arr)
    mask = (w_arr > (1.0 / 3.0)) & np.isfinite(w_arr)
    en[mask] = np.log2((1.0 + 3.0 * w_arr[mask]) / 2.0)
    return en

def parse_pgen_from_folder(folder_name):
    """Extracts pgen probability value from directory name."""
    match = re.search(r'pgen([0-9]+(?:_[0-9]+)?)', folder_name)
    return float(match.group(1).replace('_', '.')) if match else None

def find_sender_csv_files(dir_path):
    """Finds all sender timing CSV files recursively inside a folder."""
    sender_files = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            if file.lower().startswith('sender_timing') and file.lower().endswith('.csv'):
                sender_files.append(os.path.join(root, file))
    return sorted(sender_files)

def process_sender_data_pure_empirical(df, mode='one_way'):
    """Processes generation attempts using strictly valid empirical timing data."""
    df_valid = df.dropna(subset=['emit_ts_ns', 'rtt_ns', 'e2r_ns']).copy()
    df_valid = df_valid[(df_valid['rtt_ns'] > 0) & (df_valid['e2r_ns'] > 0)].sort_values('emit_ts_ns').reset_index(drop=True)

    generations = []
    current_attempts = []

    for _, row in df_valid.iterrows():
        current_attempts.append(row)
        if row.get('success_bit', 0) == 1:
            n_attempts = len(current_attempts)
            start_ts_ns = current_attempts[0]['emit_ts_ns']

            if n_attempts > 1:
                t_attempt_ns = (current_attempts[-1]['emit_ts_ns'] - start_ts_ns) / (n_attempts - 1)
            else:
                t_attempt_ns = float(current_attempts[-1]['rtt_ns'])

            end_ts_ns = current_attempts[-1]['emit_ts_ns'] + t_attempt_ns
            t_gen_s = (end_ts_ns - start_ts_ns) / 1e9

            e2r_mean_ns = float(np.mean([r['e2r_ns'] for r in current_attempts]))
            rtt_mean_ns = float(np.mean([r['rtt_ns'] for r in current_attempts]))
            t_roundtrip_ns = e2r_mean_ns if mode == 'one_way' else rtt_mean_ns

            if t_gen_s > 0 and t_roundtrip_ns > 0:
                generations.append({
                    'N_gen': n_attempts,
                    't_gen_s': t_gen_s,
                    'e2r_ns': e2r_mean_ns,
                    'rtt_ns': rtt_mean_ns,
                    't_roundtrip_ns': t_roundtrip_ns
                })
            current_attempts = []

    return pd.DataFrame(generations)

def generate_summary_table(pgen_data_dict, sorted_pgens, mode_key, mode_label, t_coh_ref_ns=1.0*1e6, w0_val=1.0, output_dir=OUTPUT_DIR):
    """Generates a summary table printed to terminal stdout and saved as a TXT file."""
    header_title = f"AEGO EXPERIMENTAL ENTANGLEMENT METRICS SUMMARY ({mode_label}, Tcoh={t_coh_ref_ns/1e6:.1f}ms, W0={w0_val:.2f})"
    divider = "=" * 115
    
    headers = [
        "pgen", "M_total", "Ngen", "tgen_mean_ms", 
        "e2r_ns_mean", "rtt_ns_mean", "EN_mean", "Rate_indiv_mean"
    ]
    
    rows = []
    for pgen in sorted_pgens:
        df_gen = pgen_data_dict[pgen]
        M_total = len(df_gen)
        Ngen = df_gen['N_gen'].mean()
        tgen_mean_ms = df_gen['t_gen_s'].mean() * 1000.0
        e2r_ns_mean = df_gen['e2r_ns'].mean()
        rtt_ns_mean = df_gen['rtt_ns'].mean()
        
        tr_ns = df_gen['e2r_ns'].values if mode_key == 'one_way' else df_gen['rtt_ns'].values
        w_vals = compute_w_experimental(tr_ns, t_coh_ref_ns, w0=w0_val)
        en_vals = compute_log_negativity(w_vals)
        en_vals[en_vals < 0.3] = 0.0  # Truncamiento a 0 si es menor a 0.3 e-bits
        
        EN_mean = float(np.mean(en_vals))
        Rate_indiv_mean = float(np.mean(en_vals / df_gen['t_gen_s'].values))
        
        rows.append([
            f"{pgen:.1f}",
            f"{M_total:d}",
            f"{Ngen:.6f}",
            f"{tgen_mean_ms:.6f}",
            f"{e2r_ns_mean:.2f}",
            f"{rtt_ns_mean:.2f}",
            f"{EN_mean:.6f}",
            f"{Rate_indiv_mean:.6f}"
        ])
    
    col_widths = [max(len(h), max(len(row[i]) for row in rows)) + 2 for i, h in enumerate(headers)]
    
    lines = [divider, header_title.center(len(divider)), divider]
    
    header_str = "".join(f"{h:^{col_widths[i]}}" for i, h in enumerate(headers))
    lines.append(header_str)
    lines.append("-" * len(divider))
    
    for row in rows:
        row_str = "".join(f"{val:^{col_widths[i]}}" for i, val in enumerate(row))
        lines.append(row_str)
        
    lines.append(divider)
    table_text = "\n".join(lines)
    
    print("\n" + table_text + "\n")
    
    file_path = os.path.join(output_dir, f"summary_table_{mode_key}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(table_text + "\n")
    print(f"[+] Summary table saved to: {file_path}")

def plot_en_vs_tcoh(en_means_matrix, en_stds_matrix, sorted_pgens, mode_label, w0_val, output_dir=OUTPUT_DIR, filename_suffix="one_way"):
    """Plots E_N vs Tcoh with error bars (standard deviation)."""
    file_path = os.path.join(output_dir, f"plot_1_EN_vs_Tcoh_{filename_suffix}.png")
    plt.figure(figsize=(9, 5.5))
    x_indices = np.arange(len(TCOH_LABELS))

    for col_idx, pgen in enumerate(sorted_pgens):
        means = en_means_matrix[:, col_idx]
        stds = en_stds_matrix[:, col_idx]
        plt.errorbar(
            x_indices, means, yerr=stds, marker='o', capsize=4, 
            label=f'$p_{{gen}} = {pgen:.1f}$', alpha=0.8, linewidth=1.5
        )

    plt.xticks(x_indices, TCOH_LABELS)
    plt.xlabel('Coherence Time ($T_{coh}$)', fontsize=11)
    plt.ylabel('Logarithmic Negativity $E_N$ [e-bits]', fontsize=11)
    plt.title(f'Logarithmic Negativity $E_N$ vs $T_{{coh}}$ ({mode_label}, Mean $\pm \sigma$)', fontsize=12, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=9)

    info_box = dict(boxstyle='round,pad=0.5', facecolor='white', edgecolor='gray', alpha=0.9)
    plt.gca().text(0.03, 0.15, f"$W_0 = {w0_val:.2f}$\nTime Mode: {mode_label}", 
                   transform=plt.gca().transAxes, fontsize=10, bbox=info_box)

    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_tgen_vs_pgen(tgen_means_ms, tgen_stds_ms, sorted_pgens, mode_label, w0_val, output_dir=OUTPUT_DIR):
    """Plots t_gen vs pgen showing mean and standard deviation."""
    file_path = os.path.join(output_dir, f"plot_2_tgen_vs_pgen.png")
    plt.figure(figsize=(8, 5))
    pgens_str = [f"{p:.1f}" for p in sorted_pgens]
    
    plt.errorbar(
        pgens_str, tgen_means_ms, yerr=tgen_stds_ms, marker='s', color='navy', 
        ecolor='crimson', capsize=5, capthick=1.5, linewidth=2, label='Empirical $t_{gen}$'
    )

    plt.xlabel('Generation Probability ($p_{gen}$)', fontsize=11)
    plt.ylabel('Generation Time $t_{gen}$ [ms]', fontsize=11)
    plt.title(f'Generation Time $t_{{gen}}$ vs $p_{{gen}}$ (Mean $\pm \sigma$)', fontsize=12, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.legend(loc='upper right', fontsize=10)


    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_ngen_vs_pgen(ngen_means, ngen_stds, sorted_pgens, mode_label, w0_val, output_dir=OUTPUT_DIR):
    """Plots N_gen vs pgen showing mean and standard deviation."""
    file_path = os.path.join(output_dir, f"plot_2b_ngen_vs_pgen.png")
    plt.figure(figsize=(8, 5))
    pgens_str = [f"{p:.1f}" for p in sorted_pgens]
    
    plt.errorbar(
        pgens_str, ngen_means, yerr=ngen_stds, marker='s', color='navy', 
        ecolor='crimson', capsize=5, capthick=1.5, linewidth=2, label='Empirical $N_{gen}$'
    )

    plt.xlabel('Generation Probability ($p_{gen}$)', fontsize=11)
    plt.ylabel('Number of Attempts $N_{gen}$', fontsize=11)
    plt.title(f'Attempts $N_{{gen}}$ vs $p_{{gen}}$ (Mean $\pm \sigma$)', fontsize=12, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.legend(loc='upper right', fontsize=10)

    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_rate_heatmap(rate_matrix, sorted_pgens, mode_label, w0_val, output_dir=OUTPUT_DIR, filename_suffix="one_way"):
    """Plots Rate Heatmap (Eq. 8) with pgen on x-axis and Tcoh on y-axis."""
    file_path = os.path.join(output_dir, f"plot_3_heatmap_Rate_vs_pgen_Tcoh_{filename_suffix}.png")
    plt.figure(figsize=(10, 6))
    pgens_str = [f"{p:.1f}" for p in sorted_pgens]

    sns.heatmap(
        rate_matrix,
        annot=True,
        fmt=".1f",
        cmap="YlOrRd",
        xticklabels=pgens_str,
        yticklabels=TCOH_LABELS,
        cbar_kws={'label': r'Individual Rate $\langle R_{indiv} \rangle$ [e-bits / s]'}
    )

    plt.xlabel('Generation Probability ($p_{gen}$)', fontsize=11, labelpad=10)
    plt.ylabel('Coherence Time ($T_{coh}$)', fontsize=11, labelpad=10)
    plt.title(r'Individual Rate Heatmap $\langle R_{\mathrm{indiv}} \rangle = \frac{1}{M} \sum \frac{E_N(i)}{t_{\mathrm{gen}}(i)}$' + '\n' +
        f'({mode_label}, $W_0={w0_val:.2f}$, $E_N = 0$ if $E_N \\leq 1/3$)', fontsize=12, fontweight='bold', pad=12)
    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_raw_en_heatmap(df_pivot, mode_label, output_dir=OUTPUT_DIR, filename_suffix="one_way"):
    """Plots raw Logarithmic Negativity E_N heatmap with warm colors."""
    file_path = os.path.join(output_dir, f"plot_4_raw_EN_heatmap_{filename_suffix}.png")
    fig, ax = plt.subplots(figsize=(10, 6))

    sns.heatmap(
        df_pivot,
        annot=True,
        fmt=".3f",
        cmap="YlOrRd",
        cbar_kws={"label": r"Logarithmic Negativity $\langle E_N \rangle$ [e-bits]"},
        ax=ax
    )

    ax.set_xlabel('Generation Probability ($p_{\mathrm{gen}}$)', fontsize=11, labelpad=10)
    ax.set_ylabel('Coherence Time ($T_{\mathrm{coh}}$)', fontsize=11, labelpad=10)
    ax.set_title(
        f"Heatmap of Raw $E_N$ vs $p_{{gen}}$ and $T_{{coh}}$ ({mode_label})",
        fontsize=11, fontweight="bold", pad=12
    )

    plt.xticks(rotation=0)
    plt.yticks(rotation=0)
    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def report_extreme_rtt_outliers(df_raw, pgen, file_path, threshold_us=500.0):
    """Identifies and prints attempt rows where RTT exceeds the specified threshold in microseconds."""
    df_check = df_raw.dropna(subset=['rtt_ns']).copy()
    df_check['rtt_us'] = df_check['rtt_ns'] / 1000.0
    outliers = df_check[df_check['rtt_us'] > threshold_us]
    
    if not outliers.empty:
        print(f"  [OUTLIER DETECTED - HIGH RTT] pgen = {pgen:.1f} | File: {os.path.basename(file_path)}")
        for idx, row in outliers.iterrows():
            print(f"      -> Attempt Row #{idx}: RTT = {row['rtt_us']:.2f} µs ({row['rtt_ns']:.0f} ns) | Success = {int(row.get('success_bit', 0))}")
    return outliers


def main():
    parser = argparse.ArgumentParser(description="AEGO Entanglement Analysis Suite")
    parser.add_argument(
        "--percentile", 
        type=float, 
        default=99.0, 
        help="Upper percentile threshold to filter t_gen_s outliers (default: 99.0)"
    )
    parser.add_argument(
        "--w0",
        type=float,
        default=1.0,
        help="Initial Werner state parameter W0 (default: 1.0)"
    )
    parser.add_argument(
        "--threshold-us",
        type=float,
        default=500.0,
        help="Microsecond (µs) threshold to alert on extreme t_gen values (default: 500.0)"
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    subdirs = [os.path.join('.', d) for d in os.listdir('.') if 'pgen' in d and os.path.isdir(os.path.join('.', d))]
    
    modes = [
        ('one_way', 'One-Way Time ($E2R$)'),
        ('roundtrip', 'Full Round-Trip Time ($RTT$)')
    ]

    already_plotted = False
    for mode_key, mode_label in modes:
        print(f"\n[*] Processing mode: {mode_label} (W0 = {args.w0:.2f})")
        pgen_data_dict = {}

        for d in sorted(subdirs):
            pgen = parse_pgen_from_folder(d)
            csv_files = find_sender_csv_files(d)
            if not csv_files or pgen is None:
                continue

            gen_dfs = []
            for f in csv_files:
                if os.path.getsize(f) > 0:
                    df_single = pd.read_csv(f)
                    df_proc = process_sender_data_pure_empirical(df_single, mode=mode_key)
                    if not df_proc.empty:
                        # Report extreme outliers before percentile filtering
                        report_extreme_rtt_outliers(df_proc, pgen, f, threshold_us=args.threshold_us)
                        gen_dfs.append(df_proc)

            if gen_dfs:
                df_gen = pd.concat(gen_dfs, ignore_index=True)
                
                if args.percentile < 100.0:
                    cutoff = np.percentile(df_gen['t_gen_s'], args.percentile)
                    df_gen = df_gen[df_gen['t_gen_s'] <= cutoff]

                if not df_gen.empty:
                    pgen_data_dict[pgen] = df_gen

        sorted_pgens = sorted(pgen_data_dict.keys())
        num_tcoh = len(TCOH_VALUES_NS)
        num_pgen = len(sorted_pgens)

        if num_pgen == 0:
            print(f"[!] No valid data found for mode {mode_key}")
            continue

        rate_matrix = np.zeros((num_tcoh, num_pgen))
        en_means_matrix = np.zeros((num_tcoh, num_pgen))
        raw_en_means_matrix = np.zeros((num_tcoh, num_pgen))
        en_stds_matrix = np.zeros((num_tcoh, num_pgen))

        tgen_means_ms = []
        tgen_stds_ms = []
        ngen_means = []
        ngen_stds = []

        for col_idx, pgen in enumerate(sorted_pgens):
            df_gen = pgen_data_dict[pgen]
            t_gen_s = df_gen['t_gen_s'].values
            n_gen_vals = df_gen['N_gen'].values
            t_roundtrip_ns = df_gen['t_roundtrip_ns'].values

            tgen_means_ms.append(np.mean(t_gen_s) * 1000.0)
            tgen_stds_ms.append(np.std(t_gen_s) * 1000.0)
            ngen_means.append(np.mean(n_gen_vals))
            ngen_stds.append(np.std(n_gen_vals))

            for row_idx, t_coh_ns in enumerate(TCOH_VALUES_NS):
                w_vals = compute_w_experimental(t_roundtrip_ns, t_coh_ns, w0=args.w0)
                en_vals_raw = compute_log_negativity(w_vals)
                
                raw_en_means_matrix[row_idx, col_idx] = np.sum(en_vals_raw) / np.sum(n_gen_vals)

                en_vals = en_vals_raw.copy()
                en_vals[en_vals < 0.3] = 0.0 
                
                en_means_matrix[row_idx, col_idx] = np.mean(en_vals)
                en_stds_matrix[row_idx, col_idx] = np.std(en_vals)

                r_indiv_gen = en_vals / t_gen_s
                rate_matrix[row_idx, col_idx] = np.mean(r_indiv_gen)

        pgens_str = [f"{p:.1f}" for p in sorted_pgens]
        df_raw_pivot = pd.DataFrame(raw_en_means_matrix, index=TCOH_LABELS, columns=pgens_str)

        plot_en_vs_tcoh(en_means_matrix, en_stds_matrix, sorted_pgens, mode_label, args.w0, filename_suffix=mode_key)
        if not already_plotted:
            plot_tgen_vs_pgen(tgen_means_ms, tgen_stds_ms, sorted_pgens, mode_label, args.w0)
            plot_ngen_vs_pgen(ngen_means, ngen_stds, sorted_pgens, mode_label, args.w0)
            already_plotted = True
        plot_rate_heatmap(rate_matrix, sorted_pgens, mode_label, args.w0, filename_suffix=mode_key)

        plot_raw_en_heatmap(df_raw_pivot, mode_label, filename_suffix=mode_key)

        generate_summary_table(pgen_data_dict, sorted_pgens, mode_key, mode_label, t_coh_ref_ns=1.0*1e6, w0_val=args.w0)

        df_res = pd.DataFrame(rate_matrix, index=TCOH_LABELS, columns=[f"pgen_{p:.1f}" for p in sorted_pgens])
        output_csv = os.path.join(OUTPUT_DIR, f"matrix_R_indiv_Tcoh_pgen_{mode_key}.csv")
        df_res.to_csv(output_csv)
        print(f"[+] Filtering applied: Percentile <= {args.percentile}%")
        print(f"[+] Rate matrix exported to: {output_csv}")


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""AESO Entanglement Swapping Analysis & Visualization Suite

Processes empirical timing data with percentile-based outlier filtering.
Saves all plots and matrices to the 'analysis_AESO' folder:
1. E_N vs Tcoh (Mean with Error Bars / Standard Deviation).
2. t_swap vs pswap (Mean and Standard Deviation).
3. Rate Heatmap (Eq. 8) as a function of pswap (x-axis) and Tcoh (y-axis).
4. Summary ASCII table printed to stdout terminal and saved as .txt file.

Generates two sets of outputs (one per client type):
- Client Lab (One-way exposure time: swap_to_recv_ns, Werner W1)
- Client Teleco (One-way exposure time: swap_to_recv_ns, Werner W2)
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
OUTPUT_DIR = "analysis_AESO"

# Coherence time sweep values (in nanoseconds) and display labels
TCOH_VALUES_NS = [
    np.inf,              # Infinite
    60.0 * 1e9,          # 1 min
    1.0 * 1e9,           # 1 s
    500.0 * 1e6,         # 500 ms
    1.0 * 1e6,           # 1 ms
    500.0 * 1e3,         # 500 us
    250.0 * 1e3,         # 250 us
    100.0 * 1e3,         # 100 us
    50.0 * 1e3,          # 50 us
    1.0 * 1e3            # 1 us
]

TCOH_LABELS = ['Infinite', '1 min', '1 s', '500 ms', '1 ms', '500 us', '250 us', '100 us', '50 us', '1 us']

def compute_w_experimental(t_exposure_ns, t_coh_ns, w0=1.00):
    """Calculates Werner state parameter W based on exposure time and Tcoh."""
    if np.isinf(t_coh_ns):
        return np.full_like(t_exposure_ns, w0)
    return w0 * np.exp(-t_exposure_ns / t_coh_ns)

def compute_log_negativity(w):
    """Calculates Logarithmic Negativity E_N(W)."""
    w_arr = np.atleast_1d(w).astype(float)
    en = np.zeros_like(w_arr)
    mask = (w_arr > (1.0 / 3.0)) & np.isfinite(w_arr)
    en[mask] = np.log2((1.0 + 3.0 * w_arr[mask]) / 2.0)
    return en

def parse_pswap_from_folder(folder_name):
    """Extracts pswap probability value from directory name (e.g. pswap0_1 -> 0.1, pswap1_0 -> 1.0)."""
    match = re.search(r'pswap([0-9]+(?:_[0-9]+)?)', folder_name)
    return float(match.group(1).replace('_', '.')) if match else None

def find_client_csv_files(dir_path, client_type='client_lab'):
    """Finds CSV files corresponding to a specific client ('client_lab' or 'client_teleco') inside a folder."""
    csv_files = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            if file.lower().endswith('.csv'):
                csv_files.append(os.path.join(root, file))
    
    csv_files = sorted(csv_files)
    if not csv_files:
        return []

    specific_matches = [
        f for f in csv_files 
        if client_type in os.path.basename(f).lower()
    ]
    return specific_matches

def process_client_data_pure_empirical(df):
    """Processes generation attempts using strictly valid empirical timing data.
    Calculates consecutive attempts N_swap and swapping time t_swap_s.
    """
    req_cols = ['ts_swap', 'swap_to_recv_ns']
    if not all(col in df.columns for col in req_cols):
        return pd.DataFrame()

    df_valid = df.dropna(subset=['ts_swap', 'swap_to_recv_ns']).copy()
    df_valid = df_valid[df_valid['swap_to_recv_ns'] > 0].sort_values('ts_swap').reset_index(drop=True)

    swapping = []
    current_attempts = []
    prev_success_ts_ns = None

    for _, row in df_valid.iterrows():
        current_attempts.append(row)
        if row.get('success_bit', 0) == 1:
            n_attempts = len(current_attempts)
            start_ts_ns = float(current_attempts[0]['ts_swap'])
            end_ts_ns = float(current_attempts[-1]['ts_swap'])

            # Calculate swap time using timestamps between successful attempts
            if prev_success_ts_ns is not None and end_ts_ns > prev_success_ts_ns:
                t_swap_s = (end_ts_ns - prev_success_ts_ns) / 1e9
            elif n_attempts > 1:
                t_step_ns = (end_ts_ns - start_ts_ns) / (n_attempts - 1)
                t_swap_s = (end_ts_ns - start_ts_ns + t_step_ns) / 1e9
            else:
                t_swap_s = float(current_attempts[-1]['swap_to_recv_ns']) / 1e9

            prev_success_ts_ns = end_ts_ns
            swap_recv_mean_ns = float(np.mean([r['swap_to_recv_ns'] for r in current_attempts]))

            if t_swap_s > 0 and swap_recv_mean_ns > 0:
                swapping.append({
                    'N_swap': n_attempts,
                    't_swap_s': t_swap_s,
                    'swap_to_recv_ns': swap_recv_mean_ns
                })
            current_attempts = []

    return pd.DataFrame(swapping)

def generate_summary_table(pswap_data_dict, sorted_pswaps, mode_key, mode_label, t_coh_ref_ns=1.0*1e6, w0_val=1.0, output_dir=OUTPUT_DIR):
    """Generates a summary table printed to terminal stdout and saved as a TXT file."""
    header_title = f"AESO EXPERIMENTAL ENTANGLEMENT METRICS SUMMARY ({mode_label}, Tcoh={t_coh_ref_ns/1e6:.1f}ms, W0={w0_val:.2f})"
    divider = "=" * 102
    
    headers = [
        "pswap", "M_total", "N_attempts_total", "Nswap", "tswap_mean_ms", 
        "swap_recv_ns_mean", "EN_mean", "Rate_indiv_mean"
    ]
    
    rows = []
    for pswap in sorted_pswaps:
        df_swap = pswap_data_dict[pswap]
        M_total = len(df_swap)
        N_attempts_total = int(df_swap['N_swap'].sum())
        Nswap = df_swap['N_swap'].mean()
        tswap_mean_ms = df_swap['t_swap_s'].mean() * 1000.0
        swap_recv_ns_mean = df_swap['swap_to_recv_ns'].mean()
        
        tr_ns = df_swap['swap_to_recv_ns'].values
        w_vals = compute_w_experimental(tr_ns, t_coh_ref_ns, w0=w0_val)
        en_vals = compute_log_negativity(w_vals)
        en_vals[en_vals < 0.3] = 0.0  # Truncamiento a 0 si es menor a 0.3 e-bits
        
        EN_mean = float(np.mean(en_vals))
        Rate_indiv_mean = float(np.mean(en_vals / df_swap['t_swap_s'].values))
        
        rows.append([
            f"{pswap:.1f}",
            f"{M_total:d}",
            f"{N_attempts_total:d}",
            f"{Nswap:.6f}",
            f"{tswap_mean_ms:.6f}",
            f"{swap_recv_ns_mean:.2f}",
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

def plot_en_vs_tcoh(en_means_matrix, en_stds_matrix, sorted_pswaps, mode_label, w0_val, output_dir=OUTPUT_DIR, filename_suffix="client_lab"):
    """Plots E_N vs Tcoh with error bars (standard deviation)."""
    file_path = os.path.join(output_dir, f"plot_1_EN_vs_Tcoh_{filename_suffix}.png")
    plt.figure(figsize=(9, 5.5))
    x_indices = np.arange(len(TCOH_LABELS))

    for col_idx, pswap in enumerate(sorted_pswaps):
        means = en_means_matrix[:, col_idx]
        stds = en_stds_matrix[:, col_idx]
        plt.errorbar(
            x_indices, means, yerr=stds, marker='o', capsize=4, 
            label=f'$p_{{swap}} = {pswap:.1f}$', alpha=0.8, linewidth=1.5
        )

    plt.xticks(x_indices, TCOH_LABELS)
    plt.xlabel('Coherence Time ($T_{coh}$)', fontsize=11)
    plt.ylabel('Logarithmic Negativity $E_N$ [e-bits]', fontsize=11)
    plt.title(f'Logarithmic Negativity $E_N$ vs $T_{{coh}}$ ({mode_label}, Mean $\pm \sigma$)', fontsize=12, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=9)

    info_box = dict(boxstyle='round,pad=0.5', facecolor='white', edgecolor='gray', alpha=0.9)
    plt.gca().text(0.03, 0.15, f"$W_0 = {w0_val:.2f}$\nClient: {mode_label}", 
                   transform=plt.gca().transAxes, fontsize=10, bbox=info_box)

    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_tswap_vs_pswap(tswap_means_ms, tswap_stds_ms, sorted_pswaps, mode_label, w0_val, output_dir=OUTPUT_DIR, filename_suffix="client_lab"):
    """Plots t_swap vs pswap showing mean and standard deviation."""
    file_path = os.path.join(output_dir, f"plot_2_tswap_vs_pswap_{filename_suffix}.png")
    plt.figure(figsize=(8, 5))
    pswaps_str = [f"{p:.1f}" for p in sorted_pswaps]
    
    plt.errorbar(
        pswaps_str, tswap_means_ms, yerr=tswap_stds_ms, marker='s', color='navy', 
        ecolor='crimson', capsize=5, capthick=1.5, linewidth=2, label='Empirical $t_{swap}$'
    )

    plt.xlabel('Swapping Probability ($p_{swap}$)', fontsize=11)
    plt.ylabel('Swap Time $t_{swap}$ [ms]', fontsize=11)
    plt.title(f'Swap Time $t_{{swap}}$ vs $p_{{swap}}$ ({mode_label}, Mean $\pm \sigma$)', fontsize=12, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.legend(loc='upper right', fontsize=10)

    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_nswap_vs_pswap(nswap_means, nswap_stds, sorted_pswaps, mode_label, w0_val, output_dir=OUTPUT_DIR, filename_suffix="client_lab"):
    """Plots N_swap vs pswap showing mean and standard deviation."""
    file_path = os.path.join(output_dir, f"plot_2b_nswap_vs_pswap_{filename_suffix}.png")
    plt.figure(figsize=(8, 5))
    pswaps_str = [f"{p:.1f}" for p in sorted_pswaps]
    
    plt.errorbar(
        pswaps_str, nswap_means, yerr=nswap_stds, marker='s', color='navy', 
        ecolor='crimson', capsize=5, capthick=1.5, linewidth=2, label='Empirical $N_{swap}$'
    )

    plt.xlabel('Swapping Probability ($p_{swap}$)', fontsize=11)
    plt.ylabel('Number of Attempts $N_{swap}$', fontsize=11)
    plt.title(f'Attempts $N_{{swap}}$ vs $p_{{swap}}$ ({mode_label}, Mean $\pm \sigma$)', fontsize=12, fontweight='bold')
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.legend(loc='upper right', fontsize=10)

    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_rate_heatmap(rate_matrix, sorted_pswaps, mode_label, w0_val, output_dir=OUTPUT_DIR, filename_suffix="client_lab"):
    """Plots Rate Heatmap (Eq. 8) with pswap on x-axis and Tcoh on y-axis."""
    file_path = os.path.join(output_dir, f"plot_3_heatmap_Rate_vs_pswap_Tcoh_{filename_suffix}.png")
    plt.figure(figsize=(10, 6))
    pswaps_str = [f"{p:.1f}" for p in sorted_pswaps]

    sns.heatmap(
        rate_matrix,
        annot=True,
        fmt=".1f",
        cmap="YlOrRd",
        xticklabels=pswaps_str,
        yticklabels=TCOH_LABELS,
        cbar_kws={'label': r'Individual Rate $\langle R_{indiv} \rangle$ [e-bits / s]'}
    )

    plt.xlabel('Swapping Probability ($p_{swap}$)', fontsize=11, labelpad=10)
    plt.ylabel('Coherence Time ($T_{coh}$)', fontsize=11, labelpad=10)
    plt.title(r'Individual Rate Heatmap $\langle R_{\mathrm{indiv}} \rangle = \frac{1}{M} \sum \frac{E_N(i)}{t_{\mathrm{swap}}(i)}$' + '\n' +
        f'({mode_label}, $W_0={w0_val:.2f}$, $E_N = 0$ if $w \\leq 1/3$)', fontsize=12, fontweight='bold', pad=12)
    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def plot_raw_en_heatmap(df_pivot, mode_label, output_dir=OUTPUT_DIR, filename_suffix="client_lab"):
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

    ax.set_xlabel('Swapping Probability ($p_{\mathrm{swap}}$)', fontsize=11, labelpad=10)
    ax.set_ylabel('Coherence Time ($T_{\mathrm{coh}}$)', fontsize=11, labelpad=10)
    ax.set_title(
        f"Heatmap of Raw $E_N$ vs $p_{{swap}}$ and $T_{{coh}}$ ({mode_label})",
        fontsize=11, fontweight="bold", pad=12
    )

    plt.xticks(rotation=0)
    plt.yticks(rotation=0)
    plt.tight_layout()
    plt.savefig(file_path, dpi=300)
    plt.close()
    print(f"[+] Plot saved: {file_path}")

def report_extreme_exposure_outliers(df_raw, pswap, file_path, threshold_us=500.0):
    """Identifies and prints attempt rows where swap_to_recv_ns exceeds threshold in microseconds."""
    df_check = df_raw.dropna(subset=['swap_to_recv_ns']).copy()
    df_check['swap_to_recv_us'] = df_check['swap_to_recv_ns'] / 1000.0
    outliers = df_check[df_check['swap_to_recv_us'] > threshold_us]
    
    if not outliers.empty:
        print(f"  [OUTLIER DETECTED - HIGH ONE-WAY TIME] pswap = {pswap:.1f} | File: {os.path.basename(file_path)}")
        for idx, row in outliers.iterrows():
            print(f"      -> Attempt Row #{idx}: Exposure = {row['swap_to_recv_us']:.2f} µs ({row['swap_to_recv_ns']:.0f} ns) | Success = {int(row.get('success_bit', 0))}")
    return outliers

def main():
    parser = argparse.ArgumentParser(description="AESO Entanglement Analysis Suite")
    parser.add_argument(
        "--percentile", 
        type=float, 
        default=99.0, 
        help="Upper percentile threshold to filter t_swap_s outliers (default: 99.0)"
    )
    parser.add_argument(
        "--w1",
        type=float,
        default=1.0,
        help="Initial Werner state parameter W0 for Client Lab (default: 1.0)"
    )
    parser.add_argument(
        "--w2",
        type=float,
        default=1.0,
        help="Initial Werner state parameter W0 for Client Teleco (default: 1.0)"
    )
    parser.add_argument(
        "--threshold-us",
        type=float,
        default=500.0,
        help="Microsecond (µs) threshold to alert on extreme t_swap values (default: 500.0)"
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    subdirs = [os.path.join('.', d) for d in os.listdir('.') if 'pswap' in d and os.path.isdir(os.path.join('.', d))]
    
    # Modes configured for the 2 Client types with their respective CSV identifiers and Werner values
    client_modes = [
        ('client_lab', 'Client Lab', 'client_lab', args.w1),
        ('client_teleco', 'Client Teleco', 'client_teleco', args.w2)
    ]

    for mode_key, mode_label, client_type, w0_val in client_modes:
        print(f"\n[*] Processing data for: {mode_label} (W0 = {w0_val:.2f})")
        pswap_data_dict = {}

        for d in sorted(subdirs):
            pswap = parse_pswap_from_folder(d)
            csv_files = find_client_csv_files(d, client_type=client_type)
            if not csv_files or pswap is None:
                continue

            swap_dfs = []
            for f in csv_files:
                if os.path.getsize(f) > 0:
                    df_single = pd.read_csv(f)
                    df_proc = process_client_data_pure_empirical(df_single)
                    if not df_proc.empty:
                        report_extreme_exposure_outliers(df_proc, pswap, f, threshold_us=args.threshold_us)
                        swap_dfs.append(df_proc)

            if swap_dfs:
                df_swap = pd.concat(swap_dfs, ignore_index=True)
                
                if args.percentile < 100.0:
                    cutoff = np.percentile(df_swap['t_swap_s'], args.percentile)
                    df_swap = df_swap[df_swap['t_swap_s'] <= cutoff]

                if not df_swap.empty:
                    pswap_data_dict[pswap] = df_swap

        sorted_pswaps = sorted(pswap_data_dict.keys())
        num_tcoh = len(TCOH_VALUES_NS)
        num_pswap = len(sorted_pswaps)

        if num_pswap == 0:
            print(f"[!] No valid data found for mode {mode_key}")
            continue

        rate_matrix = np.zeros((num_tcoh, num_pswap))
        en_means_matrix = np.zeros((num_tcoh, num_pswap))
        raw_en_means_matrix = np.zeros((num_tcoh, num_pswap))
        en_stds_matrix = np.zeros((num_tcoh, num_pswap))

        tswap_means_ms = []
        tswap_stds_ms = []
        nswap_means = []
        nswap_stds = []

        for col_idx, pswap in enumerate(sorted_pswaps):
            df_swap = pswap_data_dict[pswap]
            t_swap_s = df_swap['t_swap_s'].values
            n_swap_vals = df_swap['N_swap'].values
            swap_to_recv_ns = df_swap['swap_to_recv_ns'].values

            tswap_means_ms.append(np.mean(t_swap_s) * 1000.0)
            tswap_stds_ms.append(np.std(t_swap_s) * 1000.0)
            nswap_means.append(np.mean(n_swap_vals))
            nswap_stds.append(np.std(n_swap_vals))

            for row_idx, t_coh_ns in enumerate(TCOH_VALUES_NS):
                w_vals = compute_w_experimental(swap_to_recv_ns, t_coh_ns, w0=w0_val)
                en_vals_raw = compute_log_negativity(w_vals)
                
                raw_en_means_matrix[row_idx, col_idx] = np.sum(en_vals_raw) / np.sum(n_swap_vals)

                en_vals = en_vals_raw.copy()
                
                en_means_matrix[row_idx, col_idx] = np.mean(en_vals)
                en_stds_matrix[row_idx, col_idx] = np.std(en_vals)

                r_indiv_swap = en_vals / t_swap_s
                rate_matrix[row_idx, col_idx] = np.mean(r_indiv_swap)

        pswaps_str = [f"{p:.1f}" for p in sorted_pswaps]
        df_raw_pivot = pd.DataFrame(raw_en_means_matrix, index=TCOH_LABELS, columns=pswaps_str)

        plot_en_vs_tcoh(en_means_matrix, en_stds_matrix, sorted_pswaps, mode_label, w0_val, filename_suffix=mode_key)
        plot_tswap_vs_pswap(tswap_means_ms, tswap_stds_ms, sorted_pswaps, mode_label, w0_val, filename_suffix=mode_key)
        plot_nswap_vs_pswap(nswap_means, nswap_stds, sorted_pswaps, mode_label, w0_val, filename_suffix=mode_key)
        plot_rate_heatmap(rate_matrix, sorted_pswaps, mode_label, w0_val, filename_suffix=mode_key)
        plot_raw_en_heatmap(df_raw_pivot, mode_label, filename_suffix=mode_key)

        generate_summary_table(pswap_data_dict, sorted_pswaps, mode_key, mode_label, t_coh_ref_ns=1.0*1e6, w0_val=w0_val)

        df_res = pd.DataFrame(rate_matrix, index=TCOH_LABELS, columns=[f"pswap_{p:.1f}" for p in sorted_pswaps])
        output_csv = os.path.join(OUTPUT_DIR, f"matrix_R_indiv_Tcoh_pswap_{mode_key}.csv")
        df_res.to_csv(output_csv)
        print(f"[+] Filtering applied: Percentile <= {args.percentile}%")
        print(f"[+] Rate matrix exported to: {output_csv}")

if __name__ == '__main__':
    main()
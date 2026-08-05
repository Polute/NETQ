#!/usr/bin/env python3
"""
AEGO Entanglement Metrics Analysis (Fixed vs Empirical vs RTT-Sum Overlay Comparison)
===================================================================================
Generates overlaid plots and CSV exports comparing:
  - Fixed (Theoretical 400us default)
  - Empirical Emit-TS (Based on emit timestamps)
  - Empirical RTT-Sum (Sum of individual attempt rtt_ns)

Outputs:
  1. aego_metrics_summary.csv        (Summary table with all calculated metrics)
  2. aego_detailed_experiments.csv   (Granular per-experiment EN, t_exp, t_exp_rtt, rates)
  3. aego_metrics_comparison.png     (Line plots with 3 rate representations)
  4. aego_metrics_comparison_seq.png (Bar charts with 3 rate representations per pgen)
  5. aego_attempts_duration.png      (Average N attempts and 2 duration representations)
"""

import os
import re
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Physical default parameters in nanoseconds (ns)
T_VUELTA_DEFAULT_NS = 220000.0   # 220 us (Media global fija)
T_ATTEMPT_DEFAULT_NS = 400000.0  # 400 us (Duración de intento por defecto si no hay RTT)
TAU_RELATIVISTIC_NS = 116581.0   # Tau relativista fijo (116.581 us)

# Experimental initial fidelity and memory coherence time
W0_INITIAL = 1.00               # Initial fidelity before decay
T_MEMORY_NS = 1000000.0         # Memory coherence time (1 ms)


def parse_csv_indices(indices_str_list):
    if not indices_str_list:
        return None
    indices = set()
    for item in indices_str_list:
        for part in item.split(','):
            part = part.strip()
            if part.isdigit():
                indices.add(int(part))
    return indices if indices else None


def compute_w_experimental(t_vuelta_ns, tau_ns=TAU_RELATIVISTIC_NS, w0=W0_INITIAL, t_mem_ns=T_MEMORY_NS):
    t_total_ns = tau_ns + t_vuelta_ns
    w_exp = w0 * np.exp(-t_total_ns / t_mem_ns)
    return np.clip(w_exp, 0.0, 1.0)


def compute_log_negativity(w, n=1):
    w_arr = np.atleast_1d(w).astype(float)
    en = np.zeros_like(w_arr)
    mask = (w_arr > (1.0 / 3.0)) & np.isfinite(w_arr)
    en[mask] = n * np.log2((1.0 + 3.0 * w_arr[mask]) / 2.0)
    return en if not np.isscalar(w) else float(en[0])


def parse_pgen_from_folder(folder_name):
    match = re.search(r'pgen([0-9]+(?:_[0-9]+)?)', folder_name)
    if match:
        return float(match.group(1).replace('_', '.'))
    return None


def find_sender_csv_files(dir_path, selected_indices=None):
    sender_files = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            file_lower = file.lower()
            if file_lower.startswith('sender_timing') and file_lower.endswith('.csv'):
                sender_files.append(os.path.join(root, file))
    
    sender_files = sorted(sender_files)
    if not sender_files or selected_indices is None:
        return sender_files

    filtered_files = []
    for idx_pos, filepath in enumerate(sender_files, start=1):
        filename = os.path.basename(filepath)
        num_match = re.search(r'sender_timing_([0-9]+)\.csv', filename, re.IGNORECASE)
        run_num = int(num_match.group(1)) if num_match else None

        if (run_num is not None and run_num in selected_indices) or (idx_pos in selected_indices):
            filtered_files.append(filepath)

    return filtered_files


def process_sender_data(df):
    df = df.sort_values(by='emit_ts_ns').reset_index(drop=True)
    experiments = []
    current_attempts = []

    for _, row in df.iterrows():
        current_attempts.append(row)
        if row.get('success_bit', 0) == 1:
            n_attempts = len(current_attempts)
            start_ts_ns = current_attempts[0]['emit_ts_ns']
            
            # 1. Duración basada en Timestamps de Emisión (Emit TS)
            if n_attempts > 1:
                t_attempt_ns = (current_attempts[-1]['emit_ts_ns'] - start_ts_ns) / (n_attempts - 1)
            else:
                last_row = current_attempts[-1]
                if 'rtt_ns' in last_row and pd.notnull(last_row['rtt_ns']) and last_row['rtt_ns'] > 0:
                    t_attempt_ns = float(last_row['rtt_ns'])
                else:
                    t_attempt_ns = T_ATTEMPT_DEFAULT_NS

            end_ts_ns = current_attempts[-1]['emit_ts_ns'] + t_attempt_ns
            t_exp_s = (end_ts_ns - start_ts_ns) / 1e9

            # 2. Duración basada en la SUMA ACUMULADA del rtt_ns de CADA INTENTO
            rtt_sum_ns = 0.0
            for att in current_attempts:
                rtt_val = att.get('rtt_ns', np.nan)
                if pd.notnull(rtt_val) and rtt_val > 0:
                    rtt_sum_ns += float(rtt_val)
                else:
                    rtt_sum_ns += T_ATTEMPT_DEFAULT_NS
            t_exp_rtt_s = rtt_sum_ns / 1e9

            tv_vals_ns = [r['rtt_ns'] - r['e2r_ns'] for r in current_attempts 
                          if 'rtt_ns' in r and 'e2r_ns' in r and pd.notnull(r['rtt_ns']) and pd.notnull(r['e2r_ns'])]
            t_vuelta_ns = np.mean(tv_vals_ns) if tv_vals_ns else T_VUELTA_DEFAULT_NS

            experiments.append({
                'n_attempts': n_attempts,
                't_exp_s': t_exp_s,
                't_exp_rtt_s': t_exp_rtt_s,
                't_vuelta_ns': t_vuelta_ns
            })
            current_attempts = []

    return pd.DataFrame(experiments)


def analyze_pgen_dir(dir_path, filter_percentile=95, selected_indices=None):
    pgen = parse_pgen_from_folder(dir_path)
    csv_files = find_sender_csv_files(dir_path, selected_indices)

    if not csv_files:
        return None, None

    dfs = [pd.read_csv(f) for f in csv_files if os.path.getsize(f) > 0]
    if not dfs:
        return None, None

    df_all = pd.concat(dfs, ignore_index=True)
    df_exp = process_sender_data(df_all)

    if df_exp.empty:
        return None, None

    # Cálculos individuales por experimento
    df_exp['w_fixed'] = compute_w_experimental(T_VUELTA_DEFAULT_NS)
    df_exp['E_N_fixed'] = compute_log_negativity(df_exp['w_fixed'])

    df_exp['w_real'] = compute_w_experimental(df_exp['t_vuelta_ns'])
    df_exp['E_N_real'] = compute_log_negativity(df_exp['w_real'])

    # Métricas adicionales presentes en la tabla LaTeX
    df_exp['t_exp_theoretical_ms'] = df_exp['n_attempts'] * (T_ATTEMPT_DEFAULT_NS / 1e6)
    df_exp['rtt_emp_us'] = (df_exp['t_exp_s'] / df_exp['n_attempts']) * 1e6

    # Tasas individuales EN / texp por experimento (Emit TS vs RTT Sum)
    df_exp['rate_fixed_indiv'] = df_exp['E_N_fixed'] / df_exp['t_exp_s']
    df_exp['rate_real_indiv'] = df_exp['E_N_real'] / df_exp['t_exp_s']
    df_exp['rate_real_rtt_indiv'] = df_exp['E_N_real'] / df_exp['t_exp_rtt_s']

    cutoff = np.percentile(df_exp['t_exp_s'], filter_percentile)
    df_clean = df_exp[df_exp['t_exp_s'] <= cutoff].copy()
    df_clean['pgen'] = pgen

    t_exp_s = df_clean['t_exp_s']
    t_exp_rtt_s = df_clean['t_exp_rtt_s']
    
    t_exp_ms = t_exp_s * 1000.0
    t_exp_rtt_ms = t_exp_rtt_s * 1000.0
    total_attempts = df_clean['n_attempts'].sum()

    # Medias de Fidelidad y EN
    w_fixed_mean = np.mean(df_clean['w_fixed'])
    w_real_mean = np.mean(df_clean['w_real'])
    
    E_N_fixed_mean = np.mean(df_clean['E_N_fixed'])
    E_N_real_mean = np.mean(df_clean['E_N_real'])

    # MÉTODO 1: Throughput Físico Real (Suma EN / Suma t_exp)
    rate_fixed_throughput = df_clean['E_N_fixed'].sum() / t_exp_s.sum()
    rate_real_throughput = df_clean['E_N_real'].sum() / t_exp_s.sum()
    rate_rtt_throughput = df_clean['E_N_real'].sum() / t_exp_rtt_s.sum()

    # MÉTODO 2: Tasa Ponderada por Intento (Suma(EN / t_exp) / N_intentos)
    rate_fixed_attempt = df_clean['rate_fixed_indiv'].sum() / total_attempts
    rate_real_attempt = df_clean['rate_real_indiv'].sum() / total_attempts
    rate_rtt_attempt = df_clean['rate_real_rtt_indiv'].sum() / total_attempts

    # MÉTODO 3: Media Promedio Simple de Tasas Individuales <EN / t_exp>
    rate_fixed_indiv_mean = df_clean['rate_fixed_indiv'].mean()
    rate_real_indiv_mean = df_clean['rate_real_indiv'].mean()
    rate_rtt_indiv_mean = df_clean['rate_real_rtt_indiv'].mean()

    diff_w_pct = ((w_real_mean - w_fixed_mean) / w_fixed_mean) * 100.0
    diff_rate_tp_pct = ((rate_real_throughput - rate_fixed_throughput) / rate_fixed_throughput) * 100.0
    diff_rate_att_pct = ((rate_real_attempt - rate_fixed_attempt) / rate_fixed_attempt) * 100.0

    summary_dict = {
        'pgen': pgen,
        'folder': os.path.basename(dir_path),
        'csv_count_used': len(csv_files),
        'n_experiments_clean': len(df_clean),
        'total_attempts': total_attempts,
        'mean_N': np.mean(df_clean['n_attempts']),
        
        # Tiempos de duración de experimento y RTT (inc. métricas de tabla LaTeX)
        't_exp_theoretical_mean_ms': np.mean(df_clean['t_exp_theoretical_ms']),
        't_exp_mean_ms': np.mean(t_exp_ms),
        't_exp_rtt_mean_ms': np.mean(t_exp_rtt_ms),
        'rtt_emp_mean_us': np.mean(df_clean['rtt_emp_us']),
        
        # Medias de EN
        'E_N_fixed_mean': E_N_fixed_mean,
        'E_N_real_mean': E_N_real_mean,

        # Fidelidades y tiempos
        't_vuelta_fixed_ns': T_VUELTA_DEFAULT_NS,
        'w_exp_fixed': w_fixed_mean,
        't_vuelta_mean_ns': np.mean(df_clean['t_vuelta_ns']),
        'w_exp_real': w_real_mean,

        # Tasas
        'Rate_fixed_throughput': rate_fixed_throughput,
        'Rate_real_throughput': rate_real_throughput,
        'Rate_rtt_throughput': rate_rtt_throughput,

        'Rate_fixed_attempt': rate_fixed_attempt,
        'Rate_real_attempt': rate_real_attempt,
        'Rate_rtt_attempt': rate_rtt_attempt,

        'Rate_fixed_indiv_mean': rate_fixed_indiv_mean,
        'Rate_real_indiv_mean': rate_real_indiv_mean,
        'Rate_rtt_indiv_mean': rate_rtt_indiv_mean,

        'diff_w_%': diff_w_pct,
        'diff_Rate_throughput_%': diff_rate_tp_pct,
        'diff_Rate_attempt_%': diff_rate_att_pct
    }

    return summary_dict, df_clean


def print_t_vuelta_summary(df_res, selected_indices=None):
    indices_label = f"{sorted(list(selected_indices))}" if selected_indices else "ALL"
    print("\n" + "=" * 90)
    print(f" MEAN t_vuelta & DIFFERENCE SUMMARY PER pgen (CSV Index: {indices_label}) ")
    print("=" * 90)
    
    summary_df = pd.DataFrame({
        'pgen': df_res['pgen'],
        'csv_files': df_res['csv_count_used'],
        't_vuelta_fixed [us]': df_res['t_vuelta_fixed_ns'] / 1000.0,
        't_vuelta_real [us]': df_res['t_vuelta_mean_ns'] / 1000.0,
        't_exp_ts [ms]': df_res['t_exp_mean_ms'],
        't_exp_rtt [ms]': df_res['t_exp_rtt_mean_ms'],
        'diff_w [%]': df_res['diff_w_%'],
        'diff_Rate_TP [%]': df_res['diff_Rate_throughput_%'],
        'diff_Rate_ATT [%]': df_res['diff_Rate_attempt_%']
    })
    
    print(summary_df.to_string(index=False))
    print("=" * 90 + "\n")


def plot_overlay_comparison(df_res, output_plot):
    fig, axes = plt.subplots(1, 2, figsize=(15, 5))

    # Panel 1: Throughput Rate (3 Representaciones)
    axes[0].plot(df_res['pgen'], df_res['Rate_fixed_throughput'], 'o--', color='gray', linewidth=2, label='Rate Fixed')
    axes[0].plot(df_res['pgen'], df_res['Rate_real_throughput'], 'd-', color='#d62728', linewidth=2, label='Rate Empirical (Emit TS)')
    axes[0].plot(df_res['pgen'], df_res['Rate_rtt_throughput'], 's-', color='#2ca02c', linewidth=2, label='Rate Empirical (RTT Sum)')
    axes[0].set_xlabel(r'Generation Probability ($p_{gen}$)', fontsize=11)
    axes[0].set_ylabel(r'Rate $\sum E_N / \sum t_{exp}$ [e-bits / s]', fontsize=11)
    axes[0].set_title('Throughput Rate Comparison', fontsize=12, fontweight='bold')
    axes[0].grid(True, linestyle='--', alpha=0.6)
    axes[0].legend()

    # Panel 2: Attempt-Normalized Rate (3 Representaciones)
    axes[1].plot(df_res['pgen'], df_res['Rate_fixed_attempt'], 'o--', color='gray', linewidth=2, label='Rate Fixed')
    axes[1].plot(df_res['pgen'], df_res['Rate_real_attempt'], '^-', color='#9467bd', linewidth=2, label='Rate Empirical (Emit TS)')
    axes[1].plot(df_res['pgen'], df_res['Rate_rtt_attempt'], 's-', color='#2ca02c', linewidth=2, label='Rate Empirical (RTT Sum)')
    axes[1].plot(df_res['pgen'], df_res['Rate_real_indiv_mean'], 'x:', color='#ff7f0e', linewidth=2, label=r'$\langle R_{indiv} \rangle$ (Burst Rate)')
    axes[1].set_xlabel(r'Generation Probability ($p_{gen}$)', fontsize=11)
    axes[1].set_ylabel(r'Rate $\sum(E_N/t_{exp}) / N_{attempts}$', fontsize=11)
    axes[1].set_title('Attempt-Normalized Rate Comparison', fontsize=12, fontweight='bold')
    axes[1].grid(True, linestyle='--', alpha=0.6)
    axes[1].legend()

    plt.tight_layout()
    plt.savefig(output_plot, dpi=300)
    print(f"[+] Direct comparison plot saved to: {output_plot}")
    plt.close()


def plot_sequential_comparison(df_res, output_plot, selected_indices=None):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    x = np.arange(len(df_res))
    width = 0.25

    # Subplot 1: Rate Throughput (3 Barras)
    axes[0].bar(x - width, df_res['Rate_fixed_throughput'], width, label='Rate Fixed', color='#7f7f7f', alpha=0.85)
    axes[0].bar(x, df_res['Rate_real_throughput'], width, label='Rate Real (Emit TS)', color='#d62728', alpha=0.85)
    axes[0].bar(x + width, df_res['Rate_rtt_throughput'], width, label='Rate Real (RTT Sum)', color='#2ca02c', alpha=0.85)
    axes[0].set_title("Throughput Rate [e-bits/s]", loc='left', fontsize=11, fontweight='bold')
    axes[0].set_xlabel("pgen step", fontsize=10)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(df_res['pgen'])
    axes[0].grid(True, linestyle='--', alpha=0.5, axis='y')
    axes[0].legend()

    # Subplot 2: Rate Attempt-Normalized (3 Barras)
    axes[1].bar(x - width, df_res['Rate_fixed_attempt'], width, label='Rate Fixed', color='#7f7f7f', alpha=0.85)
    axes[1].bar(x, df_res['Rate_real_attempt'], width, label='Rate Real (Emit TS)', color='#9467bd', alpha=0.85)
    axes[1].bar(x + width, df_res['Rate_rtt_attempt'], width, label='Rate Real (RTT Sum)', color='#2ca02c', alpha=0.85)
    axes[1].set_title("Attempt-Normalized Rate", loc='left', fontsize=11, fontweight='bold')
    axes[1].set_xlabel("pgen step", fontsize=10)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(df_res['pgen'])
    axes[1].grid(True, linestyle='--', alpha=0.5, axis='y')
    axes[1].legend()

    plt.tight_layout()
    plt.savefig(output_plot, dpi=300, bbox_inches='tight')
    print(f"[+] Sequential comparison plot saved to: {output_plot}")
    plt.close()


def plot_attempts_and_duration(df_res, output_plot):
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Panel 1: Promedio de intentos (N) por pgen
    axes[0].plot(df_res['pgen'], df_res['mean_N'], 'o-', color='#1f77b4', linewidth=2, markersize=6)
    axes[0].set_xlabel(r'Generation Probability ($p_{gen}$)', fontsize=11)
    axes[0].set_ylabel(r'Average Attempts ($\langle N \rangle$)', fontsize=11)
    axes[0].set_title('Average Attempts per Experiment', fontsize=12, fontweight='bold')
    axes[0].grid(True, linestyle='--', alpha=0.6)

    # Panel 2: Duración media de los experimentos por pgen (2 representaciones)
    axes[1].plot(df_res['pgen'], df_res['t_exp_mean_ms'], 's-', color='#ff7f0e', linewidth=2, markersize=6, label=r'$\langle t_{exp} \rangle$ (Emit TS)')
    axes[1].plot(df_res['pgen'], df_res['t_exp_rtt_mean_ms'], 'd-', color='#2ca02c', linewidth=2, markersize=6, label=r'$\langle t_{exp} \rangle$ (RTT Sum)')
    axes[1].set_xlabel(r'Generation Probability ($p_{gen}$)', fontsize=11)
    axes[1].set_ylabel(r'Average Duration $\langle t_{exp} \rangle$ [ms]', fontsize=11)
    axes[1].set_title('Average Experiment Duration Comparison', fontsize=12, fontweight='bold')
    axes[1].grid(True, linestyle='--', alpha=0.6)
    axes[1].legend()

    plt.tight_layout()
    plt.savefig(output_plot, dpi=300)
    print(f"[+] Attempts and duration plot saved to: {output_plot}")
    plt.close()


def generate_all_plots(df_res, selected_indices=None):
    plot_overlay_comparison(df_res, "aego_metrics_comparison.png")
    plot_sequential_comparison(df_res, "aego_metrics_comparison_seq.png", selected_indices=selected_indices)
    plot_attempts_and_duration(df_res, "aego_attempts_duration.png")


def main():
    parser = argparse.ArgumentParser(description="AEGO Entanglement Analysis (Fixed vs Empirical vs RTT-Sum)")
    parser.add_argument("--dir", default=".", help="Data directory")
    parser.add_argument("--out", default="aego_metrics_summary.csv", help="Summary output CSV")
    parser.add_argument("--out-detailed", default="aego_detailed_experiments.csv", help="Granular per-experiment output CSV")
    parser.add_argument("--filter-p", type=float, default=95.0, help="Upper percentile cutoff for anomalies (default: 95)")
    parser.add_argument("--csv-indices", nargs="+", help="Indices of sender CSV files (e.g., --csv-indices 4 5 6)")
    parser.add_argument("--plot", action="store_true", help="Save plots")
    args = parser.parse_args()

    selected_csv_indices = parse_csv_indices(args.csv_indices)
    if selected_csv_indices:
        print(f"[*] Filtering sender files matching numbers/indices: {sorted(list(selected_csv_indices))}")

    subdirs = [os.path.join(args.dir, d) for d in os.listdir(args.dir) if 'pgen' in d and os.path.isdir(os.path.join(args.dir, d))]
    
    summary_results = []
    detailed_dfs = []

    for d in sorted(subdirs):
        summary, df_clean = analyze_pgen_dir(d, args.filter_p, selected_csv_indices)
        if summary is not None:
            summary_results.append(summary)
            detailed_dfs.append(df_clean)

    if summary_results:
        df_res = pd.DataFrame(summary_results).sort_values(by='pgen').reset_index(drop=True)
        df_res.to_csv(args.out, index=False)

        # Exportar CSV granular con experimentos individuales
        df_detailed = pd.concat(detailed_dfs, ignore_index=True)
        cols_detailed = [
            'pgen', 'n_attempts', 't_exp_s', 't_exp_rtt_s', 't_exp_theoretical_ms', 'rtt_emp_us',
            't_vuelta_ns', 'w_real', 'E_N_real', 'rate_real_indiv', 'rate_real_rtt_indiv',
            'w_fixed', 'E_N_fixed', 'rate_fixed_indiv'
        ]
        df_detailed[cols_detailed].to_csv(args.out_detailed, index=False)

        print_t_vuelta_summary(df_res, selected_csv_indices)

        print("==========================================================================================")
        print(" AEGO EXPERIMENTAL ENTANGLEMENT METRICS SUMMARY ")
        print("==========================================================================================\n")
        
        cols_to_show = [
            'pgen', 'n_experiments_clean', 'total_attempts', 'mean_N',
            't_exp_theoretical_mean_ms', 'rtt_emp_mean_us',
            'E_N_real_mean',          # Media de EN <E_N>
            'Rate_real_throughput',    # Sum(E_N) / Sum(texp) (Emit TS)
            'Rate_rtt_throughput',     # Sum(E_N) / Sum(texp_rtt) (RTT Sum)
            'Rate_real_attempt',       # Sum(E_N/texp) / N_intentos (Emit TS)
            'Rate_rtt_attempt',        # Sum(E_N/texp_rtt) / N_intentos (RTT Sum)
            'w_exp_real', 'diff_w_%'
        ]
        print(df_res[cols_to_show].to_string(index=False))
        print(f"\n[+] Summary exported to: {args.out}")
        print(f"[+] Per-experiment detailed metrics exported to: {args.out_detailed}")

        if args.plot:
            generate_all_plots(df_res, selected_indices=selected_csv_indices)


if __name__ == "__main__":
    main()
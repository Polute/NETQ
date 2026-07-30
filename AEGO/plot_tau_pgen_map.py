#!/usr/bin/env python3
"""
Plot parameter map between Tau and pgen with wdecayed as color.
Analyzes AEGO experiments with csv_pgen_0XX and json_pgen_0XX directories.
"""

import os
import sys
import json
import argparse
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path


def discover_pgen_dirs(base_dir="."):
    """Discover csv_pgen_0XX and json_pgen_0XX directories."""
    csv_dirs = []
    json_dirs = []
    
    for entry in os.listdir(base_dir):
        if entry.startswith('csv_pgen') and os.path.isdir(os.path.join(base_dir, entry)):
            csv_dirs.append(entry)
        if entry.startswith('json_pgen') and os.path.isdir(os.path.join(base_dir, entry)):
            json_dirs.append(entry)
    
    return sorted(csv_dirs), sorted(json_dirs)


def extract_pgen_from_dirname(dirname):
    """Extract pgen value from directory name like csv_pgen0_1 (0.1), csv_pgen0_2 (0.2), etc."""
    # Remove prefix
    name = dirname.replace('csv_', '').replace('json_', '')
    
    # Handle formats:
    # pgen0_1 -> 0.1
    # pgen0_2 -> 0.2
    # pgen0_10 -> 0.10
    # pgen_0XX -> 0.XX
    
    if '_' in name:
        parts = name.split('_')
        
        # pgen0_1 format (0.1, 0.2, 0.3, etc.)
        if parts[0] == 'pgen0' and len(parts) == 2:
            return float(f"0.{parts[1]}")
        
        # pgen_0XX format
        elif parts[0] == 'pgen' and len(parts) == 2:
            return float(f"0.{parts[1]}")
    
    return None


def parse_sender_csv(csv_path):
    """Parse sender CSV to extract werner, return latency (rtt_ns - e2r_ns), and success_bit."""
    try:
        df = pd.read_csv(csv_path)
        if 'werner' in df.columns and 'rtt_ns' in df.columns and 'e2r_ns' in df.columns:
            # Return latency = rtt_ns - e2r_ns
            return_latency = df['rtt_ns'].values - df['e2r_ns'].values
            result = {
                'werner': df['werner'].values,
                'return_latency_ns': return_latency,
                'rtt_ns': df['rtt_ns'].values,
                'e2r_ns': df['e2r_ns'].values,
            }
            # Add success_bit if available
            if 'success_bit' in df.columns:
                result['success_bit'] = df['success_bit'].values
            return result
        return None
    except Exception as e:
        print(f"Error parsing {csv_path}: {e}")
        return None


def parse_sender_json(json_path):
    """Parse sender JSON to extract pgen and other metadata."""
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"Error parsing {json_path}: {e}")
        return None


def compute_tau(distance_km=23.3):
    """
    Compute Tau = 2/3 * c * distance
    c = 299,792,458 m/s
    distance in km
    Returns Tau in nanoseconds
    """
    c = 299792458  # m/s
    distance_m = distance_km * 1000  # convert km to m
    tau_s = (2.0 / 3.0) * distance_m / c
    tau_ns = tau_s * 1e9  # convert to nanoseconds
    return tau_ns


def compute_wdecayed2(r2a_ns, tau_ns=116581, w0=1.0):
    """
    Compute theoretical wdecayed2:
    wdecayed1 = w0 * e^(tau/1000000)
    wdecayed2 = wdecayed1 * e^(r2a_ns/1000000)
    
    Memory time is 1,000,000 ns (1 ms)
    """
    memory_ns = 1_000_000
    wdecayed1 = w0 * np.exp(-tau_ns / memory_ns)
    wdecayed2 = wdecayed1 * np.exp(-r2a_ns / memory_ns)
    return wdecayed2


def collect_data(base_dir="."):
    """
    Collect data from all pgen directories.
    Returns list of dicts with pgen, latency stats, and werner stats.
    """
    csv_dirs, json_dirs = discover_pgen_dirs(base_dir)
    
    # Group by pgen
    pgen_data = {}
    
    # Match csv and json directories
    for csv_dir in csv_dirs:
        pgen = extract_pgen_from_dirname(csv_dir)
        if pgen is None:
            continue
        
        # Find corresponding json directory
        json_dir = csv_dir.replace('csv_', 'json_')
        if json_dir not in json_dirs:
            continue
        
        csv_path = os.path.join(base_dir, csv_dir)
        
        # Skip empty directories
        if not os.listdir(csv_path):
            print(f"Skipping empty directory: {csv_dir}")
            continue
        
        # Parse sender CSV (werner and return latency = rtt_ns - e2r_ns)
        sender_csv_files = [f for f in os.listdir(csv_path) if f.startswith('sender_timing') and f.endswith('.csv')]
        
        if not sender_csv_files:
            print(f"No sender_timing.csv found in: {csv_dir}")
            continue
        
        for csv_file in sender_csv_files:
            csv_full_path = os.path.join(csv_path, csv_file)
            csv_data = parse_sender_csv(csv_full_path)
            
            if csv_data is not None and len(csv_data['werner']) > 0:
                if pgen not in pgen_data:
                    pgen_data[pgen] = {
                        'werner': [],
                        'return_latency_ns': [],
                        'success_bit': [],
                    }
                
                pgen_data[pgen]['werner'].extend(csv_data['werner'].tolist())
                pgen_data[pgen]['return_latency_ns'].extend(csv_data['return_latency_ns'].tolist())
                
                # Add success_bit if available
                if 'success_bit' in csv_data:
                    pgen_data[pgen]['success_bit'].extend(csv_data['success_bit'].tolist())
                
                print(f"  Loaded {len(csv_data['werner'])} samples from {csv_file}")
    
    # Aggregate by pgen
    data_points = []
    tau_ns = 116581  # Fixed Tau value
    for pgen, data in pgen_data.items():
        werner = np.array(data['werner'])
        return_latency_ns = np.array(data['return_latency_ns'])
        
        # Compute theoretical wdecayed2 for each sample
        wdecayed2_theoretical = compute_wdecayed2(return_latency_ns, tau_ns, w0=1.0)
        
        # Debug: show some sample values and find anomalies
        print(f"\nDebug for pgen={pgen}:")
        print(f"  Sample 0: werner={werner[0]:.4f}, return_latency={return_latency_ns[0]:.1f}ns, wdecayed2_theoretical={wdecayed2_theoretical[0]:.4f}")
        print(f"  Sample 1: werner={werner[1]:.4f}, return_latency={return_latency_ns[1]:.1f}ns, wdecayed2_theoretical={wdecayed2_theoretical[1]:.4f}")
        print(f"  Sample 2: werner={werner[2]:.4f}, return_latency={return_latency_ns[2]:.1f}ns, wdecayed2_theoretical={wdecayed2_theoretical[2]:.4f}")
        
        # Find samples where werner < wdecayed2_theoretical (below perfect correlation)
        below_line = np.where(werner < wdecayed2_theoretical)[0]
        if len(below_line) > 0:
            print(f"  ANOMALY: {len(below_line)} samples below perfect correlation line")
            for idx in below_line[:5]:  # Show first 5
                print(f"    Sample {idx}: werner={werner[idx]:.4f}, wdecayed2_theoretical={wdecayed2_theoretical[idx]:.4f}, return_latency={return_latency_ns[idx]:.1f}ns")
        
        data_points.append({
            'pgen': pgen,
            'werner_mean': np.mean(werner),
            'werner_std': np.std(werner),
            'werner_median': np.median(werner),
            'return_latency_mean': np.mean(return_latency_ns),
            'return_latency_std': np.std(return_latency_ns),
            'return_latency_median': np.median(return_latency_ns),
            'wdecayed2_mean': np.mean(wdecayed2_theoretical),
            'wdecayed2_std': np.std(wdecayed2_theoretical),
            'wdecayed2_median': np.median(wdecayed2_theoretical),
            'werner_values': werner,
            'return_latency_values': return_latency_ns,
            'wdecayed2_values': wdecayed2_theoretical,
            'num_samples': len(werner),
        })
        
        # Add success_values if available
        if 'success_bit' in data and len(data['success_bit']) > 0:
            data_points[-1]['success_values'] = np.array(data['success_bit'])
    
    return data_points


def plot_tau_pgen_map(data_points, output_path="tau_pgen_map.png", distance_km=23.3):
    """
    Create parameter map comparing theoretical wdecayed2 vs measured werner.
    Shows how measured werner compares to theoretical prediction based on latency.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Compute Tau
    tau_ns = 116581  # Fixed Tau value
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    # Create figure
    fig, ax = plt.subplots(figsize=(12, 8))
    
    # Collect all individual data points
    all_theoretical = []
    all_measured = []
    all_pgen = []
    
    for dp in data_points:
        all_theoretical.extend(dp['wdecayed2_values'].tolist())
        all_measured.extend(dp['werner_values'].tolist())
        all_pgen.extend([dp['pgen']] * len(dp['werner_values']))
    
    # Scatter plot: theoretical vs measured
    scatter = ax.scatter(all_theoretical, all_measured, 
                        c=all_pgen, 
                        s=20, 
                        cmap='viridis',
                        vmin=0, vmax=1,
                        alpha=0.6,
                        edgecolors='none')
    
    # Add diagonal line (perfect correlation)
    max_val = max(max(all_theoretical), max(all_measured))
    ax.plot([0, max_val], [0, max_val], 'r--', linewidth=2, label='Perfect correlation')
    
    # Add colorbar for pgen
    cbar = plt.colorbar(scatter, ax=ax)
    cbar.set_label('pgen', fontsize=12)
    
    # Labels and title
    ax.set_xlabel('Theoretical wdecayed2', fontsize=14)
    ax.set_ylabel('Measured werner', fontsize=14)
    ax.set_title(f'Theoretical wdecayed2 vs Measured werner\n(Tau = {tau_ns} ns, Memory = 1 ms)', fontsize=16)
    
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, max_val * 1.1)
    ax.set_ylim(0, max_val * 1.1)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def plot_werner_vs_pgen(data_points, output_path="werner_vs_pgen.png"):
    """
    Create simple plot of werner vs return latency (rtt_ns - e2r_ns) with different series for each pgen.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    fig, ax = plt.subplots(figsize=(12, 8))
    
    # Plot each pgen as a different series
    colors = plt.cm.viridis(np.linspace(0, 1, len(data_points)))
    
    for i, dp in enumerate(data_points):
        # Scatter plot for individual points
        ax.scatter(dp['return_latency_values'], dp['werner_values'], 
                  color=colors[i], s=20, alpha=0.5, label=f'pgen={dp["pgen"]:.2f}')
        
        # Mean point
        ax.plot(dp['return_latency_mean'], dp['werner_mean'], 'o', 
               markersize=12, markeredgecolor='black', markeredgewidth=2,
               color=colors[i])
    
    ax.set_xlabel('Return Latency (rtt_ns - e2r_ns) (ns)', fontsize=14)
    ax.set_ylabel('Werner (wdecayed)', fontsize=14)
    ax.set_title('Werner vs Return Latency by pgen', fontsize=16)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def plot_werner_mean_vs_pgen(data_points, output_path="werner_mean_vs_pgen.png", tau_ns=116581):
    """
    Create plot of theoretical wdecayed2 mean vs pgen with error bars.
    Also shows wdecayed1 (tau only) as horizontal reference line.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    pgens = [dp['pgen'] for dp in data_points]
    wdecayed2_means = [dp['wdecayed2_mean'] for dp in data_points]
    wdecayed2_stds = [dp['wdecayed2_std'] for dp in data_points]
    
    # Calculate wdecayed1 (tau only)
    memory_ns = 1_000_000
    wdecayed1 = np.exp(-tau_ns / memory_ns)
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.errorbar(pgens, wdecayed2_means, yerr=wdecayed2_stds, 
                fmt='o-', capsize=5, capthick=2, markersize=8, linewidth=2, 
                label='wdecayed2 (tau + vuelta)')
    
    # Add horizontal line for wdecayed1 (tau only)
    ax.axhline(y=wdecayed1, color='r', linestyle='--', linewidth=2, 
               label=f'wdecayed1 (tau only) = {wdecayed1:.4f}')
    
    ax.set_xlabel('pgen', fontsize=14)
    ax.set_ylabel('Theoretical wdecayed (mean ± std)', fontsize=14)
    ax.set_title('Theoretical wdecayed Mean vs pgen', fontsize=16)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def plot_rtt_mean_vs_pgen(data_points, output_path="rtt_mean_vs_pgen.png"):
    """
    Create plot of RTT mean vs pgen with error bars.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    pgens = [dp['pgen'] for dp in data_points]
    rtt_means = [dp['return_latency_mean'] for dp in data_points]
    rtt_stds = [dp['return_latency_std'] for dp in data_points]
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.errorbar(pgens, rtt_means, yerr=rtt_stds, 
                fmt='o-', capsize=5, capthick=2, markersize=8, linewidth=2)
    
    ax.set_xlabel('pgen', fontsize=14)
    ax.set_ylabel('Return Latency (mean ± std) (ns)', fontsize=14)
    ax.set_title('Return Latency Mean vs pgen', fontsize=16)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def plot_werner_histograms(data_points, output_path="werner_histograms.png", tau_ns=116581):
    """
    Create histograms of theoretical wdecayed2 values for each pgen.
    Also shows wdecayed1 (tau only) as vertical reference line.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    # Calculate wdecayed1 (tau only)
    memory_ns = 1_000_000
    wdecayed1 = np.exp(-tau_ns / memory_ns)
    
    fig, ax = plt.subplots(figsize=(12, 8))
    
    colors = plt.cm.viridis(np.linspace(0, 1, len(data_points)))
    
    for i, dp in enumerate(data_points):
        ax.hist(dp['wdecayed2_values'], bins=30, alpha=0.5, 
                color=colors[i], label=f'pgen={dp["pgen"]:.2f}')
    
    # Add vertical line for wdecayed1 (tau only)
    ax.axvline(x=wdecayed1, color='r', linestyle='--', linewidth=2, 
               label=f'wdecayed1 (tau only) = {wdecayed1:.4f}')
    
    ax.set_xlabel('Theoretical wdecayed', fontsize=14)
    ax.set_ylabel('Frequency', fontsize=14)
    ax.set_title('Theoretical wdecayed Distribution by pgen', fontsize=16)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def plot_werner_boxplot(data_points, output_path="werner_boxplot.png", tau_ns=116581):
    """
    Create box plots of theoretical wdecayed2 values for each pgen.
    Also shows wdecayed1 (tau only) as horizontal reference line.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    # Calculate wdecayed1 (tau only)
    memory_ns = 1_000_000
    wdecayed1 = np.exp(-tau_ns / memory_ns)
    
    wdecayed2_values = [dp['wdecayed2_values'] for dp in data_points]
    pgen_labels = [f'{dp["pgen"]:.2f}' for dp in data_points]
    
    fig, ax = plt.subplots(figsize=(12, 8))
    
    bp = ax.boxplot(wdecayed2_values, labels=pgen_labels, patch_artist=True)
    
    # Color the boxes
    colors = plt.cm.viridis(np.linspace(0, 1, len(data_points)))
    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
    
    # Add horizontal line for wdecayed1 (tau only)
    ax.axhline(y=wdecayed1, color='r', linestyle='--', linewidth=2, 
               label=f'wdecayed1 (tau only) = {wdecayed1:.4f}')
    
    ax.set_xlabel('pgen', fontsize=14)
    ax.set_ylabel('Theoretical wdecayed', fontsize=14)
    ax.set_title('Theoretical wdecayed Box Plot by pgen', fontsize=16)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def plot_success_rate_vs_pgen(data_points, output_path="success_rate_vs_pgen.png"):
    """
    Create plot of success rate vs pgen if success_bit data is available.
    Shows success and failure percentages as annotations on each point.
    """
    if not data_points:
        print("No data points found.")
        return
    
    # Check if success data is available
    if 'success_values' not in data_points[0]:
        print("Success rate plot skipped: no success_bit data available.")
        return
    
    # Sort by pgen
    data_points = sorted(data_points, key=lambda x: x['pgen'])
    
    pgens = [dp['pgen'] for dp in data_points]
    success_rates = [np.mean(dp['success_values']) for dp in data_points]
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.plot(pgens, success_rates, 'o-', markersize=8, linewidth=2)
    
    # Add annotations with success and failure percentages
    for i, (pgen, success_rate) in enumerate(zip(pgens, success_rates)):
        success_pct = success_rate * 100
        failure_pct = (1 - success_rate) * 100
        annotation_text = f'Success: {success_pct:.1f}%\nFailure: {failure_pct:.1f}%'
        
        # Position annotation above the point
        ax.annotate(annotation_text, 
                    xy=(pgen, success_rate), 
                    xytext=(0, 10),  # 10 points offset
                    textcoords='offset points',
                    ha='center', va='bottom',
                    bbox=dict(boxstyle='round,pad=0.5', fc='yellow', alpha=0.7),
                    fontsize=9)
    
    ax.set_xlabel('pgen', fontsize=14)
    ax.set_ylabel('Success Rate', fontsize=14)
    ax.set_title('Success Rate vs pgen', fontsize=16)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved plot: {output_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Plot Tau vs pgen parameter map with wdecayed as color."
    )
    parser.add_argument("--base-dir", default=".", help="Base directory containing csv_pgen_0XX and json_pgen_0XX directories.")
    parser.add_argument("--distance-km", type=float, default=23.3, help="Distance in km for Tau calculation.")
    parser.add_argument("--output", default="tau_pgen_map.png", help="Output file for the map plot.")
    parser.add_argument("--simple-plot", default="werner_vs_pgen.png", help="Output file for simple werner vs pgen plot.")
    
    args = parser.parse_args()
    
    # Collect data
    print(f"Searching for pgen directories in: {args.base_dir}")
    data_points = collect_data(args.base_dir)
    
    if not data_points:
        print("No data found. Check that csv_pgen_0XX and json_pgen_0XX directories exist.")
        return 1
    
    print(f"Found {len(data_points)} data points:")
    for dp in data_points:
        print(f"  pgen={dp['pgen']:.2f}, werner_mean={dp['werner_mean']:.4f}, return_latency={dp['return_latency_mean']:.1f}ns, samples={dp['num_samples']}")
    
    # Generate plots
    plot_tau_pgen_map(data_points, args.output, args.distance_km)
    plot_werner_vs_pgen(data_points, args.simple_plot)
    plot_werner_mean_vs_pgen(data_points, "werner_mean_vs_pgen.png", tau_ns=116581)
    plot_rtt_mean_vs_pgen(data_points, "rtt_mean_vs_pgen.png")
    plot_werner_histograms(data_points, "werner_histograms.png", tau_ns=116581)
    plot_werner_boxplot(data_points, "werner_boxplot.png", tau_ns=116581)
    plot_success_rate_vs_pgen(data_points, "success_rate_vs_pgen.png")
    
    print("\nDone!")
    return 0


if __name__ == "__main__":
    sys.exit(main())

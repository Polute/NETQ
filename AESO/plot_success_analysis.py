#!/usr/bin/env python3
"""
Analysis of success/failure patterns and inter-success-time distribution in AESO.

Analyzes CSV files produced by AESO client with pgen/pswap probability parameters:
- Detects successful vs failed packets (based on presence in CSV vs generated count)
- Computes inter-success-time (time between consecutive successes)
- Generates histograms and statistics
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


def parse_csv(csv_path):
    """Parse AESO client delay histogram CSV file.
    
    CSV format: count_idx,delay_ns,delay_center_ns,delay_centered_ns,delay_physical_ns,clock_offset_ns,clock_sync_path_delay_ns
    
    Returns:
        list: [(count_idx, delay_ns), ...]
    """
    rows = []
    try:
        with open(csv_path, 'r') as f:
            # Skip header
            next(f)
            for line in f:
                parts = line.strip().split(',')
                if len(parts) >= 2:
                    try:
                        count_idx = int(parts[0])
                        delay_ns = int(parts[1])
                        rows.append((count_idx, delay_ns))
                    except ValueError:
                        pass
    except FileNotFoundError:
        print(f"File not found: {csv_path}", file=sys.stderr)
        return []
    return rows


def parse_json(json_path):
    """Parse JSON metadata file.
    
    Returns:
        dict: {
            'count': int (total packets),
            'warmup': int,
            'pgen': float (generation probability, default 1.0),
            'pswap': float (swap probability, default 1.0),
        }
    """
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
            args = data.get('args', {})
            return {
                'count': args.get('count', 0),
                'warmup': args.get('warmup', 0),
                'pgen': args.get('pgen', 1.0),
                'pswap': args.get('pswap', 1.0),
            }
    except (FileNotFoundError, json.JSONDecodeError):
        return {'count': 0, 'warmup': 0, 'pgen': 1.0, 'pswap': 1.0}


def detect_failures(csv_rows, total_packets, warmup):
    """Detect which packets failed (not in CSV).
    
    Args:
        csv_rows: list of (count_idx, delay_ns)
        total_packets: total packets generated
        warmup: warmup packets (excluded from analysis)
    
    Returns:
        dict: {
            'success_indices': [list of successful packet indices],
            'failure_indices': [list of failed packet indices],
            'success_count': int,
            'failure_count': int,
            'failure_rate': float,
        }
    """
    csv_indices = set(count_idx for count_idx, _ in csv_rows)
    all_indices = set(range(warmup + 1, total_packets + 1))
    
    success_indices = sorted(list(csv_indices & all_indices))
    failure_indices = sorted(list(all_indices - csv_indices))
    
    return {
        'success_indices': success_indices,
        'failure_indices': failure_indices,
        'success_count': len(success_indices),
        'failure_count': len(failure_indices),
        'failure_rate': len(failure_indices) / len(all_indices) if all_indices else 0.0,
    }


def compute_inter_success_times(csv_rows):
    """Compute inter-success-time (time between consecutive successful packets).
    
    Args:
        csv_rows: list of (count_idx, delay_ns)
    
    Returns:
        dict: {
            'inter_success_times_ns': [list of times in ns],
            'mean_ns': float,
            'median_ns': float,
            'min_ns': int,
            'max_ns': int,
            'std_ns': float,
        }
    """
    if len(csv_rows) < 2:
        return {
            'inter_success_times_ns': [],
            'mean_ns': 0.0,
            'median_ns': 0,
            'min_ns': 0,
            'max_ns': 0,
            'std_ns': 0.0,
        }
    
    # Sort by count_idx to get chronological order
    sorted_rows = sorted(csv_rows, key=lambda x: x[0])
    
    # Inter-success time is delay difference between consecutive successes
    inter_times = []
    prev_delay = sorted_rows[0][1]  # Start with first delay
    
    for i in range(1, len(sorted_rows)):
        curr_delay = sorted_rows[i][1]
        # Inter-success time as delay difference
        inter_time = abs(curr_delay - prev_delay)
        inter_times.append(inter_time)
        prev_delay = curr_delay
    
    if not inter_times:
        inter_times = [0]
    
    inter_times_sorted = sorted(inter_times)
    mean_time = sum(inter_times) / len(inter_times) if inter_times else 0.0
    median_time = inter_times_sorted[len(inter_times_sorted) // 2] if inter_times else 0
    
    # Compute std dev
    variance = sum((t - mean_time) ** 2 for t in inter_times) / len(inter_times) if inter_times else 0
    std_time = variance ** 0.5
    
    return {
        'inter_success_times_ns': inter_times,
        'mean_ns': mean_time,
        'median_ns': median_time,
        'min_ns': min(inter_times) if inter_times else 0,
        'max_ns': max(inter_times) if inter_times else 0,
        'std_ns': std_time,
    }


def detect_runs(success_indices, failure_indices, total_packets):
    """Detect consecutive runs of successes and failures.
    
    Returns:
        dict: {
            'success_runs': [list of (start, end, length)],
            'failure_runs': [list of (start, end, length)],
            'max_failure_run': int,
            'max_success_run': int,
        }
    """
    success_set = set(success_indices)
    failure_set = set(failure_indices)
    
    success_runs = []
    failure_runs = []
    
    i = 1
    while i <= total_packets:
        if i in success_set:
            start = i
            while i <= total_packets and i in success_set:
                i += 1
            success_runs.append((start, i - 1, i - start))
        elif i in failure_set:
            start = i
            while i <= total_packets and i in failure_set:
                i += 1
            failure_runs.append((start, i - 1, i - start))
        else:
            i += 1
    
    return {
        'success_runs': success_runs,
        'failure_runs': failure_runs,
        'max_failure_run': max((r[2] for r in failure_runs), default=0),
        'max_success_run': max((r[2] for r in success_runs), default=0),
    }


def plot_histograms(csv_rows, failure_data, inter_success_data, output_dir, prefix):
    """Generate and save histogram plots."""
    if not HAS_MATPLOTLIB:
        print("matplotlib not available, skipping plots")
        return
    
    os.makedirs(output_dir, exist_ok=True)
    
    # Create histograms
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    fig.suptitle(f'AESO Client Success-Failure Analysis: {prefix}', fontsize=14)
    
    delays = [row[1] for row in csv_rows]
    
    # Delay distribution
    ax = axes[0, 0]
    ax.hist(delays, bins=50, edgecolor='black', alpha=0.7)
    ax.set_title('Repeater-to-Client Delay Distribution (ns)')
    ax.set_xlabel('Delay (ns)')
    ax.set_ylabel('Count')
    
    # Success/Failure breakdown pie chart
    ax = axes[0, 1]
    sizes = [failure_data['success_count'], failure_data['failure_count']]
    labels = ['Success', 'Failure']
    colors = ['green', 'red']
    if sum(sizes) > 0:
        ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90)
        ax.set_title('Success vs Failure Breakdown')
    else:
        ax.text(0.5, 0.5, 'No data', ha='center', va='center')
    
    # Failure/Success bar chart
    ax = axes[1, 0]
    if failure_data['success_count'] + failure_data['failure_count'] > 0:
        bars = ax.bar(['Success', 'Failure'], [failure_data['success_count'], failure_data['failure_count']], color=['green', 'red'])
        ax.set_title('Success vs Failure Count')
        ax.set_ylabel('Count')
    else:
        ax.text(0.5, 0.5, 'No data', ha='center', va='center')
    ax = axes[1, 1]
    inter_times = inter_success_data.get('inter_success_times_ns', [])
    if inter_times:
        ax.hist(inter_times, bins=50, edgecolor='black', alpha=0.7, color='green')
        ax.set_title('Inter-Success-Time Distribution')
        ax.set_xlabel('Time (ns)')
        ax.set_ylabel('Count')
    else:
        ax.text(0.5, 0.5, 'No inter-success data', ha='center', va='center')
    
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, f'{prefix}_histograms.png'), dpi=100)
    plt.close()
    
    print(f"Saved plot: {output_dir}/{prefix}_histograms.png")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze AESO client success/failure patterns and inter-success-time distribution."
    )
    parser.add_argument("csv_file", help="Path to AESO client CSV output.")
    parser.add_argument("--json-file", default=None, help="Path to AESO client JSON metadata (optional).")
    parser.add_argument("--output-dir", default=".", help="Directory for output analysis and plots.")
    parser.add_argument("--prefix", default="aeso_analysis", help="Prefix for output files.")
    parser.add_argument("--plot", action="store_true", help="Generate histogram plots.")
    parser.add_argument("--quiet", action="store_true", help="Suppress console output.")
    
    args = parser.parse_args()
    
    # Parse CSV
    csv_rows = parse_csv(args.csv_file)
    if not csv_rows:
        print("No data found in CSV file.")
        return 1
    
    # Parse JSON for metadata
    json_data = {}
    if args.json_file:
        json_data = parse_json(args.json_file)
    else:
        # Try to find JSON file automatically
        csv_dir = os.path.dirname(args.csv_file) or "."
        csv_base = os.path.basename(args.csv_file)
        json_base = csv_base.replace('.csv', '.json')
        potential_json = os.path.join(csv_dir.replace('csv', 'json'), json_base)
        if os.path.exists(potential_json):
            json_data = parse_json(potential_json)
    
    # Get total packets count
    total_packets = json_data.get('count', max(row[0] for row in csv_rows) if csv_rows else 0)
    warmup = json_data.get('warmup', 0)
    pgen = json_data.get('pgen', 1.0)
    pswap = json_data.get('pswap', 1.0)
    
    # Analyze
    failure_analysis = detect_failures(csv_rows, total_packets, warmup)
    inter_success_analysis = compute_inter_success_times(csv_rows)
    runs_analysis = detect_runs(
        failure_analysis['success_indices'],
        failure_analysis['failure_indices'],
        total_packets
    )
    
    # Print results
    if not args.quiet:
        print("\n" + "="*70)
        print(f"AESO Client Success-Failure Analysis: {args.csv_file}")
        print("="*70)
        print(f"Total packets: {total_packets}")
        print(f"Warmup: {warmup}")
        print(f"pgen (generation probability): {pgen:.4f}")
        print(f"pswap (swap probability): {pswap:.4f}")
        print()
        
        print("Failure Analysis:")
        print(f"  Success count: {failure_analysis['success_count']}")
        print(f"  Failure count: {failure_analysis['failure_count']}")
        print(f"  Failure rate: {failure_analysis['failure_rate']:.4%}")
        print()
        
        print("Inter-Success-Time (time between consecutive successes):")
        print(f"  Mean: {inter_success_analysis['mean_ns']:.1f} ns")
        print(f"  Median: {inter_success_analysis['median_ns']} ns")
        print(f"  Min: {inter_success_analysis['min_ns']} ns")
        print(f"  Max: {inter_success_analysis['max_ns']} ns")
        print(f"  Std Dev: {inter_success_analysis['std_ns']:.1f} ns")
        print()
        
        print("Consecutive Runs:")
        print(f"  Max success run: {runs_analysis['max_success_run']} packets")
        print(f"  Max failure run: {runs_analysis['max_failure_run']} packets")
        print()
    
    # Save JSON report
    os.makedirs(args.output_dir, exist_ok=True)
    report = {
        'csv_file': args.csv_file,
        'total_packets': total_packets,
        'warmup': warmup,
        'pgen': pgen,
        'pswap': pswap,
        'failure_analysis': failure_analysis,
        'inter_success_analysis': inter_success_analysis,
        'runs_analysis': runs_analysis,
    }
    
    json_output = os.path.join(args.output_dir, f'{args.prefix}_analysis.json')
    with open(json_output, 'w') as f:
        json.dump(report, f, indent=2)
    
    if not args.quiet:
        print(f"Saved analysis report: {json_output}")
    
    # Generate plots if requested
    if args.plot:
        plot_histograms(csv_rows, failure_analysis, inter_success_analysis, args.output_dir, args.prefix)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

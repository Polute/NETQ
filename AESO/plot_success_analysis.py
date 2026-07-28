#!/usr/bin/env python3
"""
Analysis of success/failure patterns and inter-success-time distribution in AESO.

Analyzes CSV files produced by AESO with pswap probability parameter:
- Detects successful vs failed packets (based on pswap swap success)
- Computes inter-success-time (time between consecutive successes)
- Generates histograms and sequential plots (per-count)
- Supports 2 paths (client A and client B)
- One-way only (no RTT plots)
"""

import argparse
import csv
import json
import os
import statistics
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


def get_available_filename(base_path, extension, force=False):
    """Strict 1:1 mapping CSV-Plot. Does not append _1, _2.
    
    Args:
        base_path (str): Base path without extension.
        extension (str): File extension including dot (e.g., '.csv').
        force (bool): Kept for API compatibility, but returns the exact name regardless.
    
    Returns:
        str: Exact filename path.
    """
    return base_path + extension


def sudo_output_owner():
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return None
    try:
        return int(uid), int(gid)
    except ValueError:
        return None


def chown_output_path(path):
    owner = sudo_output_owner()
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


def chown_output_ancestors(path):
    owner = sudo_output_owner()
    if owner is None:
        return
    abs_path = os.path.abspath(path)
    roots = [os.path.abspath(os.getcwd()), os.path.abspath("/tmp")]
    for root in roots:
        if abs_path == root or not abs_path.startswith(root + os.sep):
            continue
        rel_path = os.path.relpath(abs_path, root)
        current = root
        for part in rel_path.split(os.sep):
            current = os.path.join(current, part)
            chown_output_path(current)
        break


def ensure_output_dir(directory):
    os.makedirs(directory, exist_ok=True)
    chown_output_ancestors(directory)


def savefig_owned(plt, path, **kwargs):
    plt.savefig(path, **kwargs)
    chown_output_path(path)


def parse_csv(csv_path):
    """Parse AESO client delay CSV file.
    
    Returns:
        dict: {
            'counts': [list of count indices],
            'delay_ns': [list of delay values in ns],
            'delay_physical_ns': [list of physical delay values in ns],
            'clock_offset_ns': [list of clock offset values in ns],
            'clock_sync_path_delay_ns': [list of sync path delay values in ns],
            'success': [list of success flags (1=success, 0=failure)],
            'has_success_column': bool,
        }
    """
    rows = {
        'counts': [],
        'delay_ns': [],
        'delay_physical_ns': [],
        'clock_offset_ns': [],
        'clock_sync_path_delay_ns': [],
        'success': [],
        'has_success_column': False,
    }
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            fieldnames = set(reader.fieldnames or [])
            rows['has_success_column'] = 'success' in fieldnames
            for row in reader:
                try:
                    count_idx = int(row.get('count_idx', 0))
                    # Skip negative count_idx (safety check)
                    if count_idx < 0:
                        continue
                    delay_ns = int(float(row.get('delay_ns', 0)))
                    delay_physical_ns = int(float(row.get('delay_physical_ns', 0)))
                    clock_offset_ns = int(float(row.get('clock_offset_ns', 0)))
                    clock_sync_path_delay_ns = int(float(row.get('clock_sync_path_delay_ns', 0)))
                    rows['counts'].append(count_idx)
                    rows['delay_ns'].append(delay_ns)
                    rows['delay_physical_ns'].append(delay_physical_ns)
                    rows['clock_offset_ns'].append(clock_offset_ns)
                    rows['clock_sync_path_delay_ns'].append(clock_sync_path_delay_ns)
                    if rows['has_success_column']:
                        rows['success'].append(int(row.get('success', 1)))
                except (ValueError, KeyError):
                    pass
    except FileNotFoundError:
        print(f"File not found: {csv_path}", file=sys.stderr)
        return rows
    return rows


def parse_json(json_path):
    """Parse JSON metadata file.
    
    Returns:
        dict: Metadata from JSON.
    """
    try:
        with open(json_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parse_repeater_csv(csv_path):
    """Parse AESO repeater CSV file with pswap statistics.
    
    Returns:
        dict: {
            'counts': [list of count indices],
            'swap_success': [list of success flags],
            'swap_send_time_ns': [list of send times in ns],
        }
    """
    rows = {
        'counts': [],
        'swap_success': [],
        'swap_send_time_ns': [],
    }
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    count_idx = int(row.get('count_idx', 0))
                    swap_success = int(row.get('swap_success', 1))
                    swap_send_time_ns = int(float(row.get('swap_send_time_ns', 0)))
                    rows['counts'].append(count_idx)
                    rows['swap_success'].append(swap_success)
                    rows['swap_send_time_ns'].append(swap_send_time_ns)
                except (ValueError, KeyError):
                    pass
    except FileNotFoundError:
        print(f"File not found: {csv_path}", file=sys.stderr)
        return rows
    return rows




def json_for_csv(csv_path):
    """Return parsed JSON metadata for a CSV path, using csv_* -> json_* mapping."""
    csv_dir = os.path.dirname(csv_path)
    csv_name = os.path.basename(csv_path)
    parent, dirname = os.path.split(csv_dir)
    if dirname.startswith('csv'):
        json_dirname = 'json' + dirname[3:]
        json_dir = os.path.join(parent, json_dirname) if parent else json_dirname
    else:
        json_dir = csv_dir.replace('csv', 'json', 1)
    json_path = os.path.join(json_dir, os.path.splitext(csv_name)[0] + '.json')
    return parse_json(json_path) if os.path.exists(json_path) else {}


def detect_failures_from_pswap(csv_data, total_packets):
    """Detect failures using pswap swap_success column from repeater CSV."""
    counts = csv_data['counts']
    swap_success = csv_data.get('swap_success', [])
    
    success_indices = [c for c, s in zip(counts, swap_success) if s == 1]
    failure_indices = [c for c, s in zip(counts, swap_success) if s == 0]
    
    return {
        'success_indices': success_indices,
        'failure_indices': failure_indices,
        'success_count': len(success_indices),
        'failure_count': len(failure_indices),
        'failure_rate': len(failure_indices) / len(counts) if counts else 0.0,
    }


def detect_failures_from_missing(csv_data, total_packets, warmup):
    """Detect failures using success column from CSV (like AEGO)."""
    counts = csv_data['counts']
    success_flags = csv_data.get('success', [])
    
    # Use success column if available (like AEGO)
    if csv_data.get('has_success_column') and success_flags and len(success_flags) == len(counts):
        success_indices = [c for c, s in zip(counts, success_flags) if s == 1]
        failure_indices = [c for c, s in zip(counts, success_flags) if s == 0]
    else:
        # Fallback to missing indices method
        csv_indices = set(csv_data['counts'])
        all_indices = set(range(warmup + 1, total_packets + 1))
        success_indices = sorted(list(csv_indices & all_indices))
        failure_indices = sorted(list(all_indices - csv_indices))
    
    return {
        'success_indices': success_indices,
        'failure_indices': failure_indices,
        'success_count': len(success_indices),
        'failure_count': len(failure_indices),
        'failure_rate': len(failure_indices) / (len(success_indices) + len(failure_indices)) if (len(success_indices) + len(failure_indices)) > 0 else 0.0,
    }


def compute_inter_success_times(csv_data):
    """Compute inter-success-time from CSV data (like AEGO).
    
    Computes gaps between consecutive successful counts only.
    
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
    counts = csv_data['counts']
    success_flags = csv_data.get('success', [])
    
    # Filter to only successful counts (like AEGO)
    if csv_data.get('has_success_column') and success_flags and len(success_flags) == len(counts):
        success_counts = [c for c, s in zip(counts, success_flags) if s == 1 and c > 0]
    else:
        # Fallback: use all positive counts
        success_counts = [c for c in counts if c > 0]
    
    if len(success_counts) < 2:
        return {
            'inter_success_times_ns': [],
            'mean_ns': 0.0,
            'median_ns': 0,
            'min_ns': 0,
            'max_ns': 0,
            'std_ns': 0.0,
        }
    
    # Compute gaps between consecutive successful counts
    inter_times = []
    for i in range(len(success_counts)):
        if i == 0:
            inter_times.append(0)  # First gap is 0 (like AEGO)
        else:
            inter_times.append(success_counts[i] - success_counts[i - 1])
    
    if not inter_times:
        inter_times = [0]
    
    inter_times_sorted = sorted(inter_times)
    mean_time = sum(inter_times) / len(inter_times) if inter_times else 0.0
    median_time = statistics.median(inter_times) if inter_times else 0
    
    variance = statistics.pvariance(inter_times) if len(inter_times) > 1 else 0
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
    
    i = 0
    while i < total_packets:
        if i in success_set:
            start = i
            while i < total_packets and i in success_set:
                i += 1
            success_runs.append((start, i - 1, i - start))
        elif i in failure_set:
            start = i
            while i < total_packets and i in failure_set:
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


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * fraction
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def robust_outlier_threshold(values_us, mad_scale):
    median = statistics.median(values_us)
    deviations = [abs(value - median) for value in values_us]
    mad = statistics.median(deviations)
    if mad <= 0:
        return median
    return median + mad_scale * mad


def series_summary(values):
    if not values:
        return None
    mean_value = sum(values) / len(values)
    return {
        "mean": mean_value,
        "p50": statistics.median(values),
        "p95": percentile(values, 0.95),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def add_info_box(plt, text):
    if not text:
        return
    plt.gca().text(
        0.02,
        0.98,
        text,
        transform=plt.gca().transAxes,
        va="top",
        ha="left",
        fontsize=8,
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "0.8"},
    )


def add_external_header(plt, title, info_text):
    fig = plt.gcf()
    if info_text:
        fig.text(
            0.985,
            0.995,
            info_text,
            va="top",
            ha="right",
            fontsize=7.5,
            bbox={"boxstyle": "round,pad=0.16", "facecolor": "white", "alpha": 0.92, "edgecolor": "0.8"},
        )
    fig.text(0.03, 0.845, title, va="top", ha="left", fontsize=10)


def add_reference_lines(plt, stats, orientation):
    if not stats:
        return
    if orientation == "vertical":
        if "mean" in stats:
            plt.axvline(stats["mean"], color="tab:red", linewidth=0.9, linestyle="--", label="mean")
        if "median" in stats:
            plt.axvline(stats["median"], color="tab:green", linewidth=0.9, linestyle=":", label="median")
    else:
        if "mean" in stats:
            plt.axhline(stats["mean"], color="tab:red", linewidth=0.9, linestyle="--", label="mean")
        if "median" in stats:
            plt.axhline(stats["median"], color="tab:green", linewidth=0.9, linestyle=":", label="median")


def confirm_overwrite(path, force=False):
    """Check if file exists and overwrite only if force is True."""
    if not os.path.exists(path):
        return True
    if force:
        return True
    print(f"Skipping existing file {path} (use --force to overwrite).")
    return False


def finish_plot(plt, path, title, info_text=None, force=False, overlap=False):
    if not confirm_overwrite(path, force):
        plt.close()
        return False
    if not overlap:
        add_external_header(plt, title, info_text)
        line_count = len(info_text.splitlines()) if info_text else 0
        top = 0.80 if line_count == 0 else min(0.80, 0.74 + 0.010 * line_count)
        plt.tight_layout(rect=(0.0, 0.0, 1.0, top))
    else:
        plt.title(title)
        add_info_box(plt, info_text)
        plt.tight_layout()
    savefig_owned(plt, path, dpi=150)
    plt.close()
    return True


def finish_panel_figure(fig, path, title, info_text=None, force=False, top=0.78):
    """Save a multi-panel figure with the AESO-style external header."""
    if not confirm_overwrite(path, force):
        plt.close(fig)
        return False
    if info_text:
        fig.text(
            0.985,
            0.995,
            info_text,
            va="top",
            ha="right",
            fontsize=7.5,
            bbox={"boxstyle": "round,pad=0.16", "facecolor": "white", "alpha": 0.92, "edgecolor": "0.8"},
        )
    fig.text(0.03, 0.94, title, va="top", ha="left", fontsize=10)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, top))
    savefig_owned(plt, path, dpi=150)
    plt.close(fig)
    return True


def write_basic_plots(plt, x_values, y_values, hist_path, seq_path, base, series, bins, info_text=None, reference_stats=None, plot_kind="all", force=False, convert_to_us=True):
    written = []
    # Convert to us only for time-based plots, not for count gaps
    if convert_to_us:
        y_values_display = [v / 1000.0 for v in y_values]  # Convert ns to us
    else:
        y_values_display = y_values
    
    if plot_kind in ("all", "hist"):
        plt.figure(figsize=(8, 4.6))
        plt.hist(y_values_display, bins=bins)
        add_reference_lines(plt, reference_stats, "vertical")
        plt.xlabel(series["hist_xlabel"])
        plt.ylabel("count")
        if reference_stats:
            plt.legend(loc="upper right")
        if finish_plot(plt, hist_path, f"{series['title']} histogram ({base})", info_text, force, overlap=False):
            written.append(hist_path)

    if plot_kind in ("all", "seq"):
        plt.figure(figsize=(8, 4.6))
        plt.plot(x_values, y_values_display, linewidth=0.6)
        add_reference_lines(plt, reference_stats, "horizontal")
        plt.xlabel(series["x_label"])
        plt.ylabel(series["ylabel"])
        if reference_stats:
            plt.legend(loc="upper right")
        if finish_plot(plt, seq_path, f"{series['title']} per {series['x_label']} ({base})", info_text, force, overlap=False):
            written.append(seq_path)

    return written


def write_filtered_plots(plt, x_values, y_values, filtered_hist_path, filtered_seq_path, outliers_path, base, series, bins, info_text=None, filter_threshold_us=None, mad_scale=8.0, force=False):
    y_values_us = [v / 1000.0 for v in y_values]  # Convert ns to us
    
    # Determine threshold
    if filter_threshold_us is None:
        threshold_us = robust_outlier_threshold(y_values_us, mad_scale)
    else:
        threshold_us = filter_threshold_us
    
    # Filter
    filtered_x = []
    filtered_y = []
    outlier_x = []
    outlier_y = []
    
    for x, y in zip(x_values, y_values_us):
        if y <= threshold_us:
            filtered_x.append(x)
            filtered_y.append(y)
        else:
            outlier_x.append(x)
            outlier_y.append(y)
    
    written = []
    
    # Filtered histogram
    if filtered_y:
        plt.figure(figsize=(8, 4.6))
        plt.hist(filtered_y, bins=bins)
        plt.axvline(threshold_us, color='red', linestyle='--', label=f'threshold={threshold_us:.1f}us')
        plt.xlabel(series["hist_xlabel"])
        plt.ylabel("count")
        plt.legend(loc="upper right")
        if finish_plot(plt, filtered_hist_path, f"{series['title']} histogram filtered ({base})", info_text, force, overlap=False):
            written.append(filtered_hist_path)
    
    # Filtered sequence
    if filtered_y:
        plt.figure(figsize=(8, 4.6))
        plt.plot(filtered_x, filtered_y, linewidth=0.6, label='filtered')
        if outlier_y:
            plt.scatter(outlier_x, outlier_y, s=10, color='red', label='outliers', zorder=5)
        plt.axhline(threshold_us, color='red', linestyle='--', label=f'threshold={threshold_us:.1f}us')
        plt.xlabel(series["x_label"])
        plt.ylabel(series["ylabel"])
        plt.legend(loc="upper right")
        if finish_plot(plt, filtered_seq_path, f"{series['title']} per {series['x_label']} filtered ({base})", info_text, force, overlap=False):
            written.append(filtered_seq_path)
    
    # Outliers plot
    if outlier_y:
        plt.figure(figsize=(8, 4.6))
        plt.scatter(outlier_x, outlier_y, s=15, color='red', label='outliers')
        plt.axhline(threshold_us, color='red', linestyle='--', label=f'threshold={threshold_us:.1f}us')
        plt.xlabel(series["x_label"])
        plt.ylabel(series["ylabel"])
        plt.legend(loc="upper right")
        if finish_plot(plt, outliers_path, f"{series['title']} outliers ({base})", info_text, force, overlap=False):
            written.append(outliers_path)
    
    return written




def consecutive_runs(indices):
    """Return (start, end, length) runs from sorted integer indices."""
    ordered = sorted(indices)
    if not ordered:
        return []

    runs = []
    start = ordered[0]
    prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        runs.append((start, prev, prev - start + 1))
        start = value
        prev = value
    runs.append((start, prev, prev - start + 1))
    return runs




def write_failure_plots(plt, csv_data, failure_data, sec_dir, counter_dir, prefix, info_text, total_packets, warmup, force, plot_kind):
    """Write failure distribution and explicit per-count markers."""

    failures = failure_data.get('failure_indices', [])
    if not failures:
        return

    failure_runs = consecutive_runs(failures)
    failure_lengths = [length for _, _, length in failure_runs]
    if failure_lengths and plot_kind in ("all", "hist"):
        plt.figure(figsize=(8, 4.6))
        plt.hist(failure_lengths, bins=min(50, max(1, len(set(failure_lengths)))))
        plt.xlabel("consecutive failure-run length (counts)")
        plt.ylabel("count")
        finish_plot(
            plt,
            os.path.join(sec_dir, f"{prefix}_failures.png"),
            f"Failure-run length distribution ({prefix})",
            info_text,
            force,
            overlap=False,
        )

    if plot_kind not in ("all", "seq"):
        return

    plt.figure(figsize=(8, 3.0))
    plt.scatter(failures, [1] * len(failures), color='tab:red', marker='|', s=200, label='failure')
    plt.xlim(max(0, warmup - 10), total_packets + 10)
    plt.yticks([])
    plt.xlabel("absolute count")
    plt.legend(loc="lower right")
    finish_plot(
        plt,
        os.path.join(counter_dir, f"{prefix}_failures_seq.png"),
        f"Failed counts ({prefix})",
        info_text,
        force,
        overlap=False,
    )


def build_info_text(json_data, failure_data, delay_stats=None, inter_stats=None):
    """Build info text box like AEGO."""
    info_lines = []
    
    role = json_data.get('role', 'client')
    info_lines.append(f"role: {role}")
    
    exp_name = os.path.basename(os.path.normpath(json_data.get('csv_file', '')))
    if exp_name:
        info_lines.append(f"file: {exp_name}")
    
    data_proto = json_data.get("data_protocol", "udp")
    sync_proto = json_data.get("sync_protocol")
    if not sync_proto or sync_proto == "none":
        sync_proto = "udp" if json_data.get("clock_sync", False) else "-"
    
    info_lines.append(f"data/sync={data_proto}/{sync_proto}")
    kernel_ts = json_data.get('kernel_timestamp', False)
    info_lines.append(f"kernel ts: {'yes' if kernel_ts else 'no'}")
    
    if json_data:
        args = json_data.get('args', {})
        runtime_bits = []
        if args.get('cpu') is not None:
            runtime_bits.append(f"cpu={args['cpu']}")
        if args.get('sock_buf') is not None:
            runtime_bits.append(f"buf={args['sock_buf']}")
        if args.get('busy_poll_us') is not None:
            runtime_bits.append(f"busy={args['busy_poll_us']}us")
        if runtime_bits:
            info_lines.append(" ".join(runtime_bits))
    
    if role == 'repeater':
        pswap = json_data.get('pswap', 1.0)
        info_lines.append(f"pswap: {pswap:.2f}")
    
    info_lines.append(f"total_packets: {json_data.get('total_packets', 0)}")
    info_lines.append(f"success_rate: {(1 - failure_data['failure_rate']):.2%}")
    info_lines.append(f"failure_rate: {failure_data['failure_rate']:.2%}")
    
    if delay_stats:
        delay_us = [v / 1000.0 for v in delay_stats]
        stats = series_summary(delay_us)
        if stats:
            info_lines.append(
                f"delay: mean={stats['mean']:.1f}us p50={stats['p50']:.1f}us p95={stats['p95']:.1f}us std={stats['std']:.1f}us"
            )
    
    if inter_stats:
        inter_times = inter_stats.get('inter_success_times_ns', [])
        stats = series_summary(inter_times)
        if stats:
            info_lines.append(
                f"inter-success: mean={stats['mean']:.1f}ns p50={stats['p50']:.1f}ns p95={stats['p95']:.1f}ns std={stats['std']:.1f}ns"
            )
    
    return "\n".join(info_lines)


def plot_histograms(csv_data, failure_data, inter_success_data, output_dir, prefix, json_data=None, force=False, plot_kind="all"):
    """Generate and save histogram and sequential plots for AESO (one-way delay only)."""
    if not HAS_MATPLOTLIB:
        print("matplotlib not available, skipping plots")
        return
    
    ensure_output_dir(output_dir)
    
    sec_dir = os.path.join(output_dir, "sec")
    counter_dir = os.path.join(output_dir, "counter")
    ensure_output_dir(sec_dir)
    ensure_output_dir(counter_dir)
    
    counts = csv_data['counts']
    delays = csv_data.get('delay_ns', [])
    
    # Build info text
    delay_stats = delays if delays else None
    info_text = build_info_text(json_data, failure_data, delay_stats, inter_success_data)
    
    count_axis = counts
    count_axis_label = "count"
    
    # Delay plots (one-way only)
    if delays:
        delay_us = [v / 1000.0 for v in delays]
        delay_stats = series_summary(delay_us)
        
        delay_series = {
            "title": "One-way Delay",
            "ylabel": "Delay (us)",
            "hist_xlabel": "Delay (us)",
            "x_label": count_axis_label,
        }
        
        write_basic_plots(
            plt, count_axis, delays,
            os.path.join(sec_dir, f"{prefix}_delay.png"),
            os.path.join(counter_dir, f"{prefix}_delay_seq.png"),
            prefix, delay_series, 50, info_text, delay_stats, plot_kind, force, convert_to_us=True
        )
    
    # Inter-success-time plots
    inter_times = inter_success_data.get('inter_success_times_ns', [])
    if inter_times:
        inter_stats = series_summary(inter_times)
        
        # Get success_counts for x-axis (only successful counts)
        success_flags = csv_data.get('success', [])
        if csv_data.get('has_success_column') and success_flags and len(success_flags) == len(counts):
            success_counts = [c for c, s in zip(counts, success_flags) if s == 1]
        else:
            success_counts = counts
        
        # For histogram: use gap indices as x-axis (1, 2, 3, ...)
        inter_x_hist = list(range(1, len(inter_times) + 1))
        
        # For sequential: use success_counts[1:] as x-axis (each gap ends at success count)
        # Skip first gap (0) for sequential plot to align dimensions
        inter_x_seq = success_counts[1:] if len(success_counts) > 1 else []
        inter_times_seq = inter_times[1:] if len(inter_times) > 1 else []
        
        inter_series = {
            "title": "Inter-Success-Interval",
            "ylabel": "Inter-success gap (counts)",
            "hist_xlabel": "Inter-success gap (counts)",
            "x_label": "gap index",
        }
        
        # Histogram uses gap indices
        if plot_kind in ("all", "hist"):
            write_basic_plots(
                plt, inter_x_hist, inter_times,
                os.path.join(sec_dir, f"{prefix}_inter_success.png"),
                os.path.join(sec_dir, f"{prefix}_inter_success_seq.png"),
                prefix, inter_series, 50, info_text, inter_stats, "hist", force, convert_to_us=False
            )
        
        # Sequential uses success_counts as x-axis
        if plot_kind in ("all", "seq"):
            inter_series_seq = {
                "title": "Inter-Success-Interval",
                "ylabel": "Inter-success gap (counts)",
                "hist_xlabel": "Inter-success gap (counts)",
                "x_label": count_axis_label,
            }
            write_basic_plots(
                plt, inter_x_seq, inter_times_seq,
                os.path.join(sec_dir, f"{prefix}_inter_success.png"),
                os.path.join(counter_dir, f"{prefix}_inter_success_seq.png"),
                prefix, inter_series_seq, 50, info_text, inter_stats, "seq", force, convert_to_us=False
            )
    
    # Failure plots
    total_packets = json_data.get('total_packets', max(counts) + 1 if counts else 0)
    warmup = json_data.get('warmup', 0)
    write_failure_plots(
        plt, csv_data, failure_data, sec_dir, counter_dir, prefix,
        info_text, total_packets, warmup, force, plot_kind
    )
    
    print(f"Saved plots in: {output_dir}")


def iter_csv_files(directory):
    """Iterate over CSV files in directory."""
    for filename in os.listdir(directory):
        if filename.endswith('.csv'):
            yield os.path.join(directory, filename)


def collect_jobs(inputs, output_dir):
    """Collect CSV files from input directories and map to output directory."""
    jobs = []
    if inputs:
        for input_path in inputs:
            if os.path.isdir(input_path):
                if output_dir is None:
                    output_dir = input_path
                for csv_path in iter_csv_files(input_path):
                    jobs.append((csv_path, output_dir))
            else:
                if output_dir is None:
                    csv_dir = os.path.dirname(input_path)
                    output_dir = csv_dir
                jobs.append((input_path, output_dir))
        return jobs
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Analyze AESO repeater/client success/failure patterns and inter-success-time distribution."
    )
    parser.add_argument("inputs", nargs='*', help="Path to AESO CSV directory or files.")
    parser.add_argument("--output-dir", default=None, help="Directory for output analysis and plots.")
    parser.add_argument("--plot", action="store_true", help="Generate histogram plots.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing plot files.")
    parser.add_argument("--quiet", action="store_true", help="Suppress console output.")
    
    args = parser.parse_args()
    
    # Collect CSV jobs
    jobs = collect_jobs(args.inputs, args.output_dir)
    
    if not jobs:
        print("No CSV files found.")
        return 1
    
    # Process each CSV
    for csv_path, output_dir in jobs:
        # Get JSON metadata
        json_data = json_for_csv(csv_path)
        json_data['csv_file'] = csv_path
        
        # Determine role and parse accordingly
        role = json_data.get('role', 'client')
        
        if role == 'repeater':
            # Repeater CSV with pswap
            csv_data = parse_repeater_csv(csv_path)
            pswap_stats = json_data.get('pswap_stats', {})
            total_packets = pswap_stats.get('total_swaps', len(csv_data['counts']))
            failure_data = detect_failures_from_pswap(csv_data, total_packets)
        else:
            # Client CSV
            csv_data = parse_csv(csv_path)
            args_data = json_data.get('args', {})
            total_packets = args_data.get('count', max(csv_data['counts']) if csv_data['counts'] else 0)
            warmup = args_data.get('warmup', 0)
            failure_data = detect_failures_from_missing(csv_data, total_packets, warmup)
            json_data['warmup'] = warmup
        
        inter_success_data = compute_inter_success_times(csv_data)
        
        # Print results
        if not args.quiet:
            print("\n" + "="*70)
            print(f"AESO {role.title()} Analysis: {csv_path}")
            print("="*70)
            print(f"Role: {role}")
            print(f"Total packets: {total_packets}")
            print(f"Success count: {failure_data['success_count']}")
            print(f"Failure count: {failure_data['failure_count']}")
            print(f"Failure rate: {failure_data['failure_rate']:.2%}")
            print()
        
        # Save JSON report
        ensure_output_dir(output_dir)
        prefix = os.path.splitext(os.path.basename(csv_path))[0]
        report = {
            'csv_file': csv_path,
            'role': role,
            'argv': json_data.get('argv', []),
            'args': json_data.get('args', {}),
            'created_at_unix_ns': json_data.get('created_at_unix_ns'),
            'total_packets': total_packets,
            'data_protocol': json_data.get('data_protocol', 'udp'),
            'failure_analysis': failure_data,
            'inter_success_analysis': inter_success_data,
        }
        
        if role == 'repeater':
            report.update({
                'pswap': json_data.get('pswap', 1.0),
                'pswap_stats': pswap_stats,
            })
        
        json_output = os.path.join(output_dir, f'{prefix}_analysis.json')
        with open(json_output, 'w') as f:
            json.dump(report, f, indent=2)
        
        if not args.quiet:
            print(f"Saved analysis report: {json_output}")
        
        # Generate plots if requested
        if args.plot:
            plot_histograms(csv_data, failure_data, inter_success_data, output_dir, prefix, json_data, args.force)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

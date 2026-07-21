#!/usr/bin/env python3
"""
Analysis of success/failure patterns and inter-success-time distribution in AEGO.

Analyzes CSV files produced by AEGO with pgen probability parameter:
- Detects successful vs failed packets (based on presence in CSV vs generated count)
- Computes inter-success-time (time between consecutive successes)
- Generates histograms and sequential plots (per-count)
- Supports outlier filtering similar to AESO plot_delay_hist.py style
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
    """Parse sender or receiver CSV file.
    
    Returns:
        dict: {
            'counts': [list of count indices],
            'emit_ts_ns': [list of sender emit timestamps in ns],
            'rtt_ns': [list of RTT values in ns],
            'e2r_ns': [list of E2R values in ns],
            'werner': [list of Werner values],
            'inter_success_gaps': [list of inter-success gaps from Bob],
            'success': [list of success flags (1=success, 0=failure)],
        }
    """
    rows = {
        'counts': [],
        'emit_ts_ns': [],
        'rtt_ns': [],
        'e2r_ns': [],
        'werner': [],
        'inter_success_gaps': [],
        'success': [],
        'has_inter_success_gap': False,
        'has_success_column': False,
    }
    try:
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            fieldnames = set(reader.fieldnames or [])
            rows['has_inter_success_gap'] = 'inter_success_gap' in fieldnames
            rows['has_success_column'] = 'success' in fieldnames
            for row in reader:
                try:
                    idx = int(row.get('count_index', row.get('index', row.get('count_idx', 0))))
                    emit_ts = row.get('emit_ts_ns', '')
                    emit_ts_ns = int(emit_ts) if emit_ts not in ('', None) else None
                    rtt = int(float(row.get('rtt_ns', row.get('delay_ns', 0))))
                    e2r = int(float(row.get('e2r_ns', row.get('s2r_ns', 0))))
                    werner = float(row.get('werner', 0.0))
                    rows['counts'].append(idx)
                    rows['emit_ts_ns'].append(emit_ts_ns)
                    rows['rtt_ns'].append(rtt)
                    rows['e2r_ns'].append(e2r)
                    rows['werner'].append(werner)
                    if rows['has_inter_success_gap']:
                        rows['inter_success_gaps'].append(int(float(row.get('inter_success_gap', 0))))
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


def paired_receiver_csv(sender_csv_path):
    """Return the receiver CSV that matches a sender CSV, if it exists."""
    directory = os.path.dirname(sender_csv_path)
    filename = os.path.basename(sender_csv_path)
    if 'sender' not in filename:
        return None
    candidate = os.path.join(directory, filename.replace('sender', 'receiver', 1))
    return candidate if os.path.exists(candidate) else None


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


def detect_failures(csv_data, total_packets, warmup):
    """Detect failures using success column from CSV.
    
    Returns:
        dict: {
            'success_indices': [list of successful packet indices],
            'failure_indices': [list of failed packet indices],
            'success_count': int,
            'failure_count': int,
            'failure_rate': float,
        }
    """
    counts = csv_data['counts']
    success_flags = csv_data.get('success', [])
    
    # Use success column if available
    if csv_data.get('has_success_column') and success_flags and len(success_flags) == len(counts):
        success_indices = [c for c, s in zip(counts, success_flags) if s == 1]
        failure_indices = [c for c, s in zip(counts, success_flags) if s == 0]
    else:
        # Fallback to old method (missing indices)
        csv_indices = set(csv_data['counts'])
        all_indices = set(range(warmup, total_packets))
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
    """Compute inter-success-time from CSV data (Bob's perspective).
    
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
    inter_times = csv_data.get('inter_success_gaps', [])
    gaps_are_aligned = (
        csv_data.get('has_inter_success_gap')
        and len(inter_times) == len(counts)
        and (not inter_times or inter_times[0] == 0)
    )

    if not gaps_are_aligned:
        inter_times = []
        for i, count_idx in enumerate(counts):
            if i == 0:
                inter_times.append(0)
            else:
                inter_times.append(count_idx - counts[i - 1])
    
    if not inter_times:
        inter_times = []
    
    inter_times_sorted = sorted(inter_times)
    mean_time = sum(inter_times) / len(inter_times) if inter_times else 0.0
    median_time = statistics.median(inter_times) if inter_times else 0
    
    # Compute std dev
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


def sender_maps(csv_data):
    """Build per-count sender timing maps."""
    counts = csv_data.get('counts', [])
    return {
        'emit_ts_ns': {
            count_idx: value
            for count_idx, value in zip(counts, csv_data.get('emit_ts_ns', []))
            if value is not None
        },
        'rtt_ns': dict(zip(counts, csv_data.get('rtt_ns', []))),
        'e2r_ns': dict(zip(counts, csv_data.get('e2r_ns', []))),
    }


def compute_failure_recovery_segments(csv_data, failure_data, total_packets, warmup):
    """Compute recovery segments for failure runs that end in a Bob success.

    Segment 1: first failed sender emission -> Bob computes next success.
    Segment 2: Bob computes that success -> success ack reaches sender.
    """
    maps = sender_maps(csv_data)
    emit_by_count = maps['emit_ts_ns']
    rtt_by_count = maps['rtt_ns']
    e2r_by_count = maps['e2r_ns']
    success_set = set(failure_data.get('success_indices', []))
    failure_set = set(failure_data.get('failure_indices', []))
    segments = []
    failure_start = None

    for count_idx in range(warmup, total_packets):
        if count_idx in failure_set:
            if failure_start is None:
                failure_start = count_idx
            continue

        if count_idx in success_set:
            if failure_start is not None:
                if (
                    failure_start in emit_by_count
                    and count_idx in emit_by_count
                    and count_idx in rtt_by_count
                    and count_idx in e2r_by_count
                ):
                    start_emit_ns = emit_by_count[failure_start]
                    success_emit_ns = emit_by_count[count_idx]
                    e2r_ns = max(0, e2r_by_count[count_idx])
                    rtt_ns = max(0, rtt_by_count[count_idx])
                    segment1_ns = max(0, success_emit_ns - start_emit_ns + e2r_ns)
                    segment2_ns = max(0, rtt_ns - e2r_ns)
                    segments.append({
                        'failure_start': failure_start,
                        'success_count': count_idx,
                        'failure_len': count_idx - failure_start,
                        'failure_start_ts_ns': start_emit_ns,
                        'segment1_ns': segment1_ns,
                        'segment2_ns': segment2_ns,
                        'total_ns': segment1_ns + segment2_ns,
                    })
                failure_start = None

    return segments


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


def write_rtt_gap_plots(plt, csv_data, failure_data, sec_dir, counter_dir, prefix, info_text, total_packets, warmup, force, plot_kind):
    """Write RTT distribution and per-count plot with Bob failures shown as gaps."""

    maps = sender_maps(csv_data)
    rtt_by_count = maps['rtt_ns']
    success_set = set(failure_data.get('success_indices', []))
    full_counts = [count_idx for count_idx in range(warmup, total_packets) if count_idx in rtt_by_count]
    if not full_counts:
        return

    success_rtts_us = [
        rtt_by_count[count_idx] / 1000.0
        for count_idx in full_counts
        if count_idx in success_set
    ]
    if success_rtts_us and plot_kind in ("all", "hist"):
        plt.figure(figsize=(8, 4.6))
        plt.hist(success_rtts_us, bins=50)
        plt.xlabel("RTT for Bob successes (us)")
        plt.ylabel("count")
        finish_plot(
            plt,
            os.path.join(sec_dir, f"{prefix}_rtt_gaps.png"),
            f"RTT distribution excluding Bob failure gaps ({prefix})",
            info_text,
            force,
            overlap=False,
        )

    if plot_kind not in ("all", "seq"):
        return

    rtt_with_gaps_us = [
        rtt_by_count[count_idx] / 1000.0 if count_idx in success_set else np.nan
        for count_idx in full_counts
    ]

    plt.figure(figsize=(8, 4.6))
    plt.plot(full_counts, rtt_with_gaps_us, linewidth=0.6, color='tab:blue')
    plt.xlabel("absolute count")
    plt.ylabel("RTT (us)")
    finish_plot(
        plt,
        os.path.join(counter_dir, f"{prefix}_rtt_seq_gaps.png"),
        f"RTT per absolute count with Bob failure gaps ({prefix})",
        info_text,
        force,
        overlap=False,
    )


def write_failure_plots(plt, csv_data, failure_data, sec_dir, counter_dir, prefix, info_text, total_packets, warmup, force, plot_kind):
    """Write Bob failure distribution and explicit per-count markers."""

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
            f"Bob failure-run length distribution ({prefix})",
            info_text,
            force,
            overlap=False,
        )

    if plot_kind not in ("all", "seq"):
        return

    plt.figure(figsize=(8, 3.0))
    plt.scatter(failures, [1] * len(failures), color='tab:red', marker='|', s=200, label='Bob failure')
    plt.xlim(max(0, warmup - 10), total_packets + 10)
    plt.yticks([])
    plt.xlabel("absolute count")
    plt.legend(loc="lower right")
    finish_plot(
        plt,
        os.path.join(counter_dir, f"{prefix}_failures_seq.png"),
        f"Bob failed counts ({prefix})",
        info_text,
        force,
        overlap=False,
    )


def write_failure_recovery_segment_plots(plt, csv_data, failure_data, sec_dir, counter_dir, prefix, info_text, total_packets, warmup, force, plot_kind):
    """Write two-segment recovery distributions and per-count sequence plots."""

    segments = compute_failure_recovery_segments(csv_data, failure_data, total_packets, warmup)
    if not segments:
        return

    failure_counts = [segment['failure_start'] for segment in segments]
    segment1_us = [segment['segment1_ns'] / 1000.0 for segment in segments]
    segment2_us = [segment['segment2_ns'] / 1000.0 for segment in segments]
    total_us = [segment['total_ns'] / 1000.0 for segment in segments]

    if plot_kind in ("all", "hist"):
        fig, axes = plt.subplots(2, 1, figsize=(8, 6.2))
        axes[0].hist(segment1_us, bins=50, color='tab:blue')
        axes[0].set_ylabel("count")
        axes[0].set_title("first failure -> Bob success")
        axes[1].hist(segment2_us, bins=50, color='tab:orange')
        axes[1].set_xlabel("duration (us)")
        axes[1].set_ylabel("count")
        axes[1].set_title("Bob success -> sender ack")
        finish_panel_figure(
            fig,
            os.path.join(sec_dir, f"{prefix}_failure_recovery_segments.png"),
            f"Failure recovery segment distributions ({prefix})",
            info_text,
            force,
            top=0.78,
        )

    if plot_kind not in ("all", "seq"):
        return

    fig, axes = plt.subplots(2, 1, figsize=(8, 6.2), sharex=True)
    axes[0].plot(failure_counts, segment1_us, linewidth=0.7, color='tab:blue')
    axes[0].scatter(failure_counts, segment1_us, color='tab:blue', s=10, zorder=3)
    axes[0].set_ylabel("duration (us)")
    axes[0].set_title("first failure -> Bob success")
    axes[1].plot(failure_counts, segment2_us, linewidth=0.7, color='tab:orange')
    axes[1].scatter(failure_counts, segment2_us, color='tab:orange', s=10, zorder=3)
    axes[1].set_xlabel("failure start count")
    axes[1].set_ylabel("duration (us)")
    axes[1].set_title("Bob success -> sender ack")
    finish_panel_figure(
        fig,
        os.path.join(counter_dir, f"{prefix}_failure_recovery_segments_seq.png"),
        f"Failure recovery segments per count ({prefix})",
        info_text,
        force,
        top=0.78,
    )

    segment_specs = [
        ("segment1", segment1_us, "tab:blue", "first failure -> Bob success"),
        ("segment2", segment2_us, "tab:orange", "Bob success -> sender ack"),
    ]
    for suffix, values, color, label in segment_specs:
        plt.figure(figsize=(8, 4.6))
        plt.plot(failure_counts, values, linewidth=0.7, color=color)
        plt.scatter(failure_counts, values, color=color, s=10, zorder=3)
        plt.xlabel("failure start count")
        plt.ylabel("duration (us)")
        finish_plot(
            plt,
            os.path.join(counter_dir, f"{prefix}_failure_recovery_{suffix}_seq.png"),
            f"Failure recovery {label} per count ({prefix})",
            info_text,
            force,
            overlap=False,
        )


def write_sender_component_plots(plt, x_values, outbound_us, remainder_us, total_us, hist_path, seq_path, base, series, info_text=None, plot_kind="all", force=False):
    """Write sender-only path component plots."""
    written = []

    components = [
        ("ida / outbound", outbound_us, "tab:blue"),
        ("resto (proc+vuelta)", remainder_us, "tab:orange"),
        ("total (ida+proc+vuelta)", total_us, "tab:green"),
    ]

    if plot_kind in ("all", "hist"):
        plt.figure(figsize=(10, 8))
        fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=False)
        for axis, (label, values, color) in zip(axes, components):
            axis.hist(values, bins=50, edgecolor="black", alpha=0.75, color=color)
            axis.set_title(label)
            axis.set_xlabel("time (us)")
            axis.set_ylabel("count")
        fig.suptitle(f"AEGO sender path components ({base})", fontsize=11, fontweight="bold")
        if info_text:
            fig.text(
                0.02,
                0.98,
                info_text,
                transform=fig.transFigure,
                va="top",
                ha="left",
                fontsize=8,
                bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "0.8"},
            )
        fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.86))
        if confirm_overwrite(hist_path, force):
            savefig_owned(plt=plt, path=hist_path, dpi=150)
            written.append(hist_path)
        plt.close()

    if plot_kind in ("all", "seq"):
        fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
        for axis, (label, values, color) in zip(axes, components):
            axis.plot(x_values, values, linewidth=0.7, color=color)
            axis.set_title(label)
            axis.set_ylabel("time (us)")
        axes[-1].set_xlabel(series["x_label"])
        fig.suptitle(f"AEGO sender path components per {series['x_label']} ({base})", fontsize=11, fontweight="bold")
        if info_text:
            fig.text(
                0.02,
                0.98,
                info_text,
                transform=fig.transFigure,
                va="top",
                ha="left",
                fontsize=8,
                bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "0.8"},
            )
        fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.86))
        if confirm_overwrite(seq_path, force):
            savefig_owned(plt=plt, path=seq_path, dpi=150)
            written.append(seq_path)
        plt.close()

    return written
def plot_gap_durations(csv_data, output_dir, prefix, json_data, info_text, force, plot_kind="all"):
    """Plot the time duration of count gaps (jumps) both by time (sequential) and count index."""
    if not HAS_MATPLOTLIB:
        return
    
    counts = csv_data['counts']
    emit_ts_ns = csv_data.get('emit_ts_ns', [])
    
    has_emit_ts = len(emit_ts_ns) == len(counts) and all(ts is not None for ts in emit_ts_ns)
    if not has_emit_ts or len(counts) < 2:
        print("Skipping gap duration plots: missing emission timestamps or insufficient packets.")
        return

    # Encontrar los saltos (donde el salto de count sea mayor a 1)
    gap_counts = []
    gap_times_us = []
    gap_durations_us = []
    
    base_emit_ts = emit_ts_ns[0]
    
    for i in range(1, len(counts)):
        gap_size = counts[i] - counts[i-1]
        if gap_size > 1:
            # Duración real en microsegundos del salto temporal
            duration_us = (emit_ts_ns[i] - emit_ts_ns[i-1]) / 1000.0
            gap_durations_us.append(duration_us)
            
            # Eje X basado en Counts (ID del paquete donde se recupera el flujo)
            gap_counts.append(counts[i])
            
            # Eje X basado en Tiempo Secuencial (us desde el primer paquete enviado)
            time_us = (emit_ts_ns[i] - base_emit_ts) / 1000.0
            gap_times_us.append(time_us)
            
    if not gap_durations_us:
        print("No count gaps (jumps) detected. Skipping gap duration plots.")
        return

    # Definir carpetas de destino tradicionales
    counter_dir = os.path.join(output_dir, "counter")
    sec_dir = os.path.join(output_dir, "sec")
    ensure_output_dir(counter_dir)
    ensure_output_dir(sec_dir)

    # 1. GRÁFICA SECUENCIAL (Eje X: Tiempo de la simulación)
    if plot_kind in ("all", "seq"):
        plt.figure(figsize=(8, 4.6))
        # Dibujamos líneas desde el suelo (Y=0) hasta el pico para que sea un spike limpio
        plt.vlines(gap_times_us, 0, gap_durations_us, colors='tab:red', alpha=0.5, linewidth=1.0)
        plt.scatter(gap_times_us, gap_durations_us, color='tab:red', s=25, zorder=3, label='Gap Duration')
        
        plt.xlabel("sender ts since first emit (us)")
        plt.ylabel("Gap Duration (us)")
        plt.legend(loc="upper right")
        
        gap_dur_seq_path = os.path.join(sec_dir, f"{prefix}_gap_durations_seq.png")
        finish_plot(plt, gap_dur_seq_path, f"Gap Duration per sequential time ({prefix})", info_text, force, overlap=False)

    # 2. GRÁFICA POR COUNT (Eje X: Absolute Count)
    if plot_kind in ("all", "seq"):
        plt.figure(figsize=(8, 4.6))
        plt.vlines(gap_counts, 0, gap_durations_us, colors='tab:red', alpha=0.5, linewidth=1.0)
        plt.scatter(gap_counts, gap_durations_us, color='tab:red', s=25, zorder=3, label='Gap Duration')
        
        plt.xlabel("Absolute Count")
        plt.ylabel("Gap Duration (us)")
        plt.legend(loc="upper right")
        
        gap_dur_cnt_path = os.path.join(counter_dir, f"{prefix}_gap_durations_count.png")
        finish_plot(plt, gap_dur_cnt_path, f"Gap Duration per absolute count ({prefix})", info_text, force, overlap=False)

def plot_histograms(csv_data, failure_data, inter_success_data, output_dir, prefix, bins=50, filtered=False, filter_threshold_us=None, mad_scale=8.0, plot_kind="all", json_data=None, force=False, inter_source_data=None):
    """Generate and save histogram and sequential plots.
    
    Shows:
    - RTT (round-trip latency) - histogram and sequential by count
    - Inter-success-time (space between successful counts) - histogram and sequential
    - RTT with gaps, explicit failures, and two-segment failure recovery plots
    """
    if not HAS_MATPLOTLIB:
        print("matplotlib not available, skipping plots")
        return
    
    ensure_output_dir(output_dir)
    
    sec_dir = os.path.join(output_dir, "sec")
    counter_dir = os.path.join(output_dir, "counter")
    sec_filtered_dir = os.path.join(output_dir, "sec_filtered")
    counter_filtered_dir = os.path.join(output_dir, "counter_filtered")
    counter_outliers_dir = os.path.join(output_dir, "counter_outliers")
    
    ensure_output_dir(sec_dir)
    ensure_output_dir(counter_dir)
    if filtered:
        ensure_output_dir(sec_filtered_dir)
        ensure_output_dir(counter_filtered_dir)
        ensure_output_dir(counter_outliers_dir)
    
    counts = csv_data['counts']
    emit_ts_ns = csv_data.get('emit_ts_ns', [])
    rtts = csv_data['rtt_ns']
    inter_source_data = inter_source_data or csv_data
    
    total_packets = json_data.get('total_packets', max(counts) + 1 if counts else 0)
    warmup = json_data.get('warmup', 0)

    count_axis = counts
    count_axis_label = "count"
    
    # Build info text with full metadata like AESO
    info_lines = []
    mode = json_data.get('mode', 'unknown') if json_data else 'unknown'
    info_lines.append(f"mode: {mode}")
    exp_name = os.path.basename(os.path.normpath(output_dir))
    if exp_name:
        info_lines.append(f"exp: {exp_name}")
    data_proto = json_data.get("protocol", json_data.get("data_protocol", "udp"))
    sync_proto = json_data.get("sync_protocol")
    if not sync_proto or sync_proto == "none":
        sync_proto = "udp" if json_data.get("clock_sync", False) else "-"

    info_lines.append(f"data/sync={data_proto}/{sync_proto}")
    kernel_ts = json_data.get('kernel_timestamp', False) if json_data else False
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
    
    if json_data:
        args = json_data.get('args', {})
        pace_mode = args.get('pace_mode')
        count_interval = args.get('count_interval')
        if pace_mode or count_interval not in (None, 0, 0.0):
            interval_str = f"{count_interval:.3g}s" if count_interval else "-"
            info_lines.append(f"pace={pace_mode or '-'}@{interval_str}")
    
    info_lines.append(f"total_packets: {len(counts)}")
    info_lines.append(f"success_rate: {(1 - failure_data['failure_rate']):.2%}")
    info_lines.append(f"failure_rate: {failure_data['failure_rate']:.2%}")
    
    if rtts:
        rtt_us = [v / 1000.0 for v in rtts]
        rtt_stats = series_summary(rtt_us)
        if rtt_stats:
            info_lines.append(
                f"rtt: mean={rtt_stats['mean']:.1f}us p50={rtt_stats['p50']:.1f}us p95={rtt_stats['p95']:.1f}us std={rtt_stats['std']:.1f}us"
            )
    
    if json_data:
        pgen = json_data.get('pgen', 1.0)
        info_lines.append(f"pgen: {pgen:.2f}")
    
    info_text = "\n".join(info_lines)
    
    rtt_series = {
        "title": "RTT",
        "ylabel": "RTT (us)",
        "hist_xlabel": "RTT (us)",
        "x_label": count_axis_label,
    }
    
    inter_times = inter_success_data.get('inter_success_times_ns', [])
    inter_counts = inter_source_data.get('counts', [])
    if len(inter_counts) == len(inter_times):
        inter_x_values = inter_counts
        inter_x_label = "Bob success count" if inter_source_data is not csv_data else "success count"
    else:
        inter_x_values = list(range(len(inter_times)))
        inter_x_label = "success index"
    inter_series = {
        "title": "Inter-Success-Interval",
        "ylabel": "Inter-success gap (counts)",
        "hist_xlabel": "Inter-success gap (counts)",
        "x_label": inter_x_label,
    }
    
    if rtts:
        rtt_stats = series_summary([v / 1000.0 for v in rtts])
        write_basic_plots(
            plt, count_axis, rtts,
            os.path.join(sec_dir, f"{prefix}_rtt.png"),
            os.path.join(counter_dir, f"{prefix}_rtt_seq.png"),
            prefix, rtt_series, bins, info_text, rtt_stats, plot_kind, force, convert_to_us=True
        )
    
    if inter_times:
        inter_times_display = inter_times
        inter_stats = series_summary(inter_times)
        write_basic_plots(
            plt, inter_x_values, inter_times_display,
            os.path.join(sec_dir, f"{prefix}_inter_success.png"),
            os.path.join(counter_dir, f"{prefix}_inter_success_seq.png"),
            prefix, inter_series, bins, info_text, inter_stats, plot_kind, force, convert_to_us=False
        )

    write_rtt_gap_plots(
        plt, csv_data, failure_data, sec_dir, counter_dir, prefix,
        info_text, total_packets, warmup, force, plot_kind
    )
    write_failure_plots(
        plt, csv_data, failure_data, sec_dir, counter_dir, prefix,
        info_text, total_packets, warmup, force, plot_kind
    )
    write_failure_recovery_segment_plots(
        plt, csv_data, failure_data, sec_dir, counter_dir, prefix,
        info_text, total_packets, warmup, force, plot_kind
    )
    
    if filtered:
        if rtts:
            write_filtered_plots(
                plt, counts, rtts,
                os.path.join(sec_filtered_dir, f"{prefix}_rtt_filtered.png"),
                os.path.join(counter_filtered_dir, f"{prefix}_rtt_seq_filtered.png"),
                os.path.join(counter_outliers_dir, f"{prefix}_rtt_seq_outliers.png"),
                prefix, rtt_series, bins, info_text, filter_threshold_us, mad_scale, force
            )
    
    print(f"Saved plots in: {output_dir}")


def collect_jobs(inputs, output_dir):
    """Collect CSV files from input directories and map to output directory."""
    jobs = []
    if inputs:
        for input_path in inputs:
            if os.path.isdir(input_path):
                if output_dir is None:
                    if is_csv_dir(input_path):
                        output_dir = csv_dir_to_plots_dir(input_path)
                    else:
                        output_dir = input_path
                for csv_path in iter_csv_files(input_path):
                    jobs.append((csv_path, output_dir))
            else:
                if output_dir is None:
                    csv_dir = os.path.dirname(input_path)
                    if is_csv_dir(csv_dir):
                        output_dir = csv_dir_to_plots_dir(csv_dir)
                    else:
                        output_dir = csv_dir
                jobs.append((input_path, output_dir))
        return jobs
    
    csv_dirs = discover_csv_dirs()
    if not csv_dirs:
        return []
    for csv_root in csv_dirs:
        plots_root = csv_dir_to_plots_dir(csv_root)
        for csv_path in iter_csv_files(csv_root):
            jobs.append((csv_path, plots_root))
    return jobs


def iter_csv_files(directory):
    for filename in os.listdir(directory):
        if filename.endswith('.csv'):
            yield os.path.join(directory, filename)


def discover_csv_dirs():
    csv_dirs = []
    for entry in os.listdir('.'):
        if entry.startswith('csv_') and os.path.isdir(entry):
            csv_dirs.append(entry)
    return sorted(csv_dirs)


def csv_dir_to_plots_dir(csv_dir):
    basename = os.path.basename(csv_dir)
    if basename.startswith('csv_'):
        return 'plots_' + basename[4:]
    return basename


def is_csv_dir(path):
    basename = os.path.basename(path)
    return basename.startswith('csv_') and os.path.isdir(path)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze AEGO success/failure patterns and inter-success-time distribution with AESO-style plots."
    )
    parser.add_argument("csv", nargs="*", help="CSV files or csv* directories produced by minimal_epr_fast.py --plot")
    parser.add_argument("--output-dir", default=None, help="Directory for output analysis and plots. If not specified, uses plots_* matching csv_*.")
    parser.add_argument("--prefix", default=None, help="Override output prefix (no extension).")
    parser.add_argument("--plot", dest="plot", action="store_true", default=True, help="Generate histogram and sequential plots.")
    parser.add_argument("--no-plot", dest="plot", action="store_false", help="Skip plots.")
    parser.add_argument("--json", dest="json_output", action=argparse.BooleanOptionalAction, default=False, help="Write JSON analysis report.")
    parser.add_argument("--quiet", action="store_true", help="Suppress console output.")
    parser.add_argument("--bins", type=int, default=50, help="Number of bins for histograms.")
    parser.add_argument("--filtered", action="store_true", help="Also write filtered plots with outlier removal.")
    parser.add_argument("--filter-threshold-us", type=float, default=None, help="Delay threshold in us for filtered plots. Defaults to median + mad-scale*MAD.")
    parser.add_argument("--mad-scale", type=float, default=8.0, help="MAD multiplier used for automatic filtered-plot threshold.")
    parser.add_argument("--plot-kind", choices=("all", "hist", "seq"), default="all", help="Limit plots to histogram, sequential, or both.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing plot files without prompting.")
    
    args = parser.parse_args()
    
    jobs = collect_jobs(args.csv, args.output_dir)
    if not jobs:
        print("No CSV files found.")
        return 1
    
    for csv_path, output_dir in jobs:
        # Process sender files (RTT data), skip receiver files
        if 'receiver' in os.path.basename(csv_path).lower():
            if not args.quiet:
                print(f"Skipping receiver file (use sender for RTT analysis): {csv_path}")
            continue

        csv_data = parse_csv(csv_path)
        if not csv_data['counts']:
            print(f"No data found in CSV file: {csv_path}")
            continue
        
        base = os.path.splitext(os.path.basename(csv_path))[0]
        prefix = args.prefix if args.prefix else base
        
        sender_json_data = json_for_csv(csv_path)
        receiver_csv_path = paired_receiver_csv(csv_path)
        receiver_data = None
        receiver_json_data = {}
        if receiver_csv_path:
            receiver_data = parse_csv(receiver_csv_path)
            receiver_json_data = json_for_csv(receiver_csv_path)
            if not receiver_data['counts']:
                receiver_data = None
        analysis_data = receiver_data or csv_data
        json_data = dict(sender_json_data)
        if receiver_json_data:
            json_data['receiver'] = receiver_json_data
            for key in ('total_packets', 'warmup'):
                if key not in json_data and receiver_json_data.get(key) is not None:
                    json_data[key] = receiver_json_data[key]
            json_data['pgen'] = receiver_json_data.get('pgen', json_data.get('pgen', 1.0))
            json_data['receiver_kernel_timestamp'] = receiver_json_data.get('kernel_timestamp', False)
            json_data['receiver_success_count'] = receiver_json_data.get('success_count')
        
        total_packets = json_data.get('total_packets', max(csv_data['counts']) + 1 if csv_data['counts'] else 0)
        warmup = json_data.get('warmup', 0)
        pgen = json_data.get('pgen', 1.0)
        
        failure_analysis = detect_failures(analysis_data, total_packets, warmup)
        inter_success_analysis = compute_inter_success_times(analysis_data)
        runs_analysis = detect_runs(
            failure_analysis['success_indices'],
            failure_analysis['failure_indices'],
            total_packets
        )
        
        if not args.quiet:
            print("\n" + "="*70)
            print(f"AEGO Success-Failure Analysis: {csv_path}")
            print("="*70)
            print(f"Output dir: {output_dir}")
            if receiver_csv_path and receiver_data:
                print(f"Receiver sidecar: {receiver_csv_path}")
            print(f"Total packets: {total_packets}")
            print(f"Warmup: {warmup}")
            print(f"pgen (Bob success probability): {pgen:.4f}")
            print()
            
            print("Failure Analysis:")
            print(f"  Success count: {failure_analysis['success_count']}")
            print(f"  Failure count: {failure_analysis['failure_count']}")
            print(f"  Failure rate: {failure_analysis['failure_rate']:.4%}")
            print()
            
            print("Inter-Success Gap (Bob count distance between successes):")
            print(f"  Mean: {inter_success_analysis['mean_ns']:.2f} counts")
            print(f"  Median: {inter_success_analysis['median_ns']} counts")
            print(f"  Min: {inter_success_analysis['min_ns']} counts")
            print(f"  Max: {inter_success_analysis['max_ns']} counts")
            print(f"  Std Dev: {inter_success_analysis['std_ns']:.2f} counts")
            print()
            
            print("Consecutive Runs:")
            print(f"  Max success run: {runs_analysis['max_success_run']} packets")
            print(f"  Max failure run: {runs_analysis['max_failure_run']} packets")
            print()
        
        os.makedirs(output_dir, exist_ok=True)

        if args.plot:
            plot_histograms(
                csv_data, failure_analysis, inter_success_analysis, output_dir, prefix,
                bins=args.bins, filtered=args.filtered, filter_threshold_us=args.filter_threshold_us,
                mad_scale=args.mad_scale, plot_kind=args.plot_kind, json_data=json_data, force=args.force,
                inter_source_data=analysis_data
            )

        if args.json_output:
            report = {
                'csv_file': csv_path,
                'receiver_csv_file': receiver_csv_path,
                'total_packets': total_packets,
                'warmup': warmup,
                'pgen': pgen,
                'failure_analysis': failure_analysis,
                'inter_success_analysis': inter_success_analysis,
                'runs_analysis': runs_analysis,
            }

            json_output = os.path.join(output_dir, f'{prefix}_analysis.json')
            with open(json_output, 'w') as f:
                json.dump(report, f, indent=2)

            if not args.quiet:
                print(f"Saved analysis report: {json_output}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

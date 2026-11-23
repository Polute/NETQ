#!/usr/bin/env python3
"""
Analyze post-stall success fractions in AEGSO generation CSV files.

This script uses only the Python standard library. It can be copied to an
offline Linux machine and run with:

    python3 analyze_frac_post.py /path/to/aegso_reports

The expected CSV columns are:
    count, ts_gen, ts_rtt, w_rtt, bit_success_gen

For each CSV:
    gap_ms = (ts_gen[i] - ts_gen[i-1]) / 1e6
    warm_success = successful attempt and gap_ms < stall_threshold_ms
    frac_post = post_stall_successes / successful_attempts

A success is post-stall when gap_ms >= stall_threshold_ms. This is identical
to 1 - frac(warm_successes), for successful attempts only.
"""

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys


STALL_THRESHOLD_MS_DEFAULT = 2000.0
REQUIRED_COLUMNS = {"ts_gen", "bit_success_gen"}


def parse_decimal_token(value):
    """Convert a path token such as 0_5 or 1.0 to a float."""
    value = value.strip().lower()
    value = value.replace(",", ".")
    value = value.replace("_", ".")
    try:
        return float(value)
    except ValueError:
        return None


def infer_parameter(path, name):
    """Infer pgen or pswap from directory names or a matching JSON metadata file."""
    pattern = re.compile(r"(?:^|[^a-z0-9])" + re.escape(name) +
                         r"[_-]?([0-9]+(?:[._][0-9]+)?)",
                         re.IGNORECASE)

    normalized_parts = path.replace("\\", "/").split("/")
    for part in reversed(normalized_parts):
        match = pattern.search(part)
        if match:
            value = parse_decimal_token(match.group(1))
            if value is not None:
                return value

    stem = os.path.splitext(path)[0]
    for candidate in (path[:-4] + ".json", stem + ".json",
                      os.path.join(os.path.dirname(path), os.path.basename(stem) + ".json")):
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                if name in metadata:
                    return float(metadata[name])
            except (OSError, ValueError, json.JSONDecodeError):
                pass

    return None


def infer_node(path):
    """Infer lab/teleco/node label from the filename or parent directories."""
    text = path.lower()
    if "teleco" in text:
        return "teleco"
    if "lab" in text:
        return "lab"
    return "unknown"


def infer_filename_csvs(root):
    """Find generation CSV files recursively."""
    if os.path.isfile(root):
        return [root] if root.lower().endswith(".csv") else []

    matches = []
    for directory, _, filenames in os.walk(root):
        for filename in filenames:
            if not filename.lower().endswith(".csv"):
                continue
            if "_gen" not in filename.lower():
                continue
            matches.append(os.path.join(directory, filename))
    return sorted(matches)


def mean(values):
    return statistics.fmean(values) if values else None


def median(values):
    return statistics.median(values) if values else None


def pstdev(values):
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def analyze_csv(path, stall_threshold_ms, forced_pswap, forced_pgen):
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("missing CSV header")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError("missing columns: " + ", ".join(sorted(missing)))
        rows = list(reader)

    samples = []
    for row_index, row in enumerate(rows, start=2):
        try:
            ts_gen = int(float(row["ts_gen"]))
            success = int(float(row["bit_success_gen"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid row %d: %s" % (row_index, error))
        samples.append((ts_gen, success))

    samples.sort(key=lambda item: item[0])

    successful_gaps = []
    post_stall_successes = 0
    warm_successes = 0

    previous_ts = None
    for ts_gen, success in samples:
        if previous_ts is None:
            gap_ms = 0.0
        else:
            gap_ms = (ts_gen - previous_ts) / 1_000_000.0

        if success == 1:
            successful_gaps.append(gap_ms)
            if gap_ms >= stall_threshold_ms:
                post_stall_successes += 1
            else:
                warm_successes += 1
        previous_ts = ts_gen

    n_attempts = len(samples)
    n_successes = len(successful_gaps)
    frac_post = post_stall_successes / n_successes if n_successes else 0.0
    m_prime = (1.0 - frac_post) * n_attempts

    max_gap_ms = max(successful_gaps) if successful_gaps else 0.0
    mean_success_gap_ms = mean(successful_gaps)

    pswap = forced_pswap if forced_pswap is not None else infer_parameter(path, "pswap")
    pgen = forced_pgen if forced_pgen is not None else infer_parameter(path, "pgen")

    return {
        "file": path,
        "node": infer_node(path),
        "pswap": pswap,
        "pgen": pgen,
        "M_attempts": n_attempts,
        "n_successes": n_successes,
        "warm_successes": warm_successes,
        "post_stall_successes": post_stall_successes,
        "frac_post": frac_post,
        "M_prime": m_prime,
        "mean_success_gap_ms": mean_success_gap_ms,
        "max_success_gap_ms": max_gap_ms,
        "stall_threshold_ms": stall_threshold_ms,
    }


def format_optional(value):
    if value is None:
        return ""
    return "%.3f" % value


def write_individual_csv(output_path, records):
    fieldnames = [
        "file", "node", "pswap", "pgen", "M_attempts", "n_successes",
        "warm_successes", "post_stall_successes", "frac_post", "M_prime",
        "mean_success_gap_ms", "max_success_gap_ms", "stall_threshold_ms",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = dict(record)
            for field in ("pswap", "pgen", "frac_post", "M_prime",
                          "mean_success_gap_ms", "max_success_gap_ms",
                          "stall_threshold_ms"):
                row[field] = format_optional(row[field])
            writer.writerow(row)


def write_group_csv(output_path, records):
    groups = {}
    for record in records:
        key = (record["pswap"], record["pgen"], record["node"])
        groups.setdefault(key, []).append(record)

    fieldnames = [
        "pswap", "pgen", "node", "n_csv",
        "M_attempts_total", "n_successes_total",
        "frac_post_mean", "frac_post_median", "frac_post_std",
        "frac_post_min", "frac_post_max", "frac_post_range",
        "M_prime_mean", "M_prime_median", "post_successes_total",
        "warm_successes_total",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for key in sorted(groups, key=lambda item:
                          ((item[0] is None, item[0] if item[0] is not None else 0),
                           (item[1] is None, item[1] if item[1] is not None else 0),
                           item[2])):
            pswap, pgen, node = key
            group = groups[key]
            values = [record["frac_post"] for record in group]
            m_prime_values = [record["M_prime"] for record in group]
            row = {
                "pswap": "" if pswap is None else "%.3f" % pswap,
                "pgen": "" if pgen is None else "%.3f" % pgen,
                "node": node,
                "n_csv": len(group),
                "M_attempts_total": sum(record["M_attempts"] for record in group),
                "n_successes_total": sum(record["n_successes"] for record in group),
                "frac_post_mean": "%.6f" % mean(values),
                "frac_post_median": "%.6f" % median(values),
                "frac_post_std": "%.6f" % pstdev(values),
                "frac_post_min": "%.6f" % min(values),
                "frac_post_max": "%.6f" % max(values),
                "frac_post_range": "%.6f" % (max(values) - min(values)),
                "M_prime_mean": "%.3f" % mean(m_prime_values),
                "M_prime_median": "%.3f" % median(m_prime_values),
                "post_successes_total": sum(record["post_stall_successes"] for record in group),
                "warm_successes_total": sum(record["warm_successes"] for record in group),
            }
            writer.writerow(row)


def write_console_summary(records):
    print("\nPost-stall fraction analysis")
    print("===========================")
    print("M = generation attempts")
    print("frac_post = successful attempts with gap >= threshold / successful attempts")
    print("M_prime = (1 - frac_post) * M")
    print("")

    groups = {}
    for record in records:
        key = (record["pswap"], record["pgen"], record["node"])
        groups.setdefault(key, []).append(record)

    for key in sorted(groups, key=lambda item:
                      ((item[0] is None, item[0] if item[0] is not None else 0),
                       (item[1] is None, item[1] if item[1] is not None else 0),
                       item[2])):
        pswap, pgen, node = key
        group = groups[key]
        values = [record["frac_post"] for record in group]
        print("pswap=%s pgen=%s node=%s n_csv=%d M=%d frac_post mean=%.6f median=%.6f std=%.6f range=[%.6f, %.6f]" %
              ("" if pswap is None else "%.3f" % pswap,
               "" if pgen is None else "%.3f" % pgen,
               node,
               len(group),
               sum(record["M_attempts"] for record in group),
               mean(values),
               median(values),
               pstdev(values),
               min(values),
               max(values)))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Analyze frac_post independently for each AEGSO generation CSV."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="CSV file or directory tree to scan (default: current directory)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default="frac_post_analysis",
        help="Directory for generated CSV reports",
    )
    parser.add_argument(
        "--stall-threshold-ms",
        type=float,
        default=STALL_THRESHOLD_MS_DEFAULT,
        help="Gap threshold for post-stall successes (default: 2000 ms)",
    )
    parser.add_argument(
        "--pswap",
        type=float,
        default=None,
        help="Override pswap for every scanned CSV",
    )
    parser.add_argument(
        "--pgen",
        type=float,
        default=None,
        help="Override pgen for every scanned CSV",
    )
    return parser


def main():
    args = build_parser().parse_args()

    if args.stall_threshold_ms < 0:
        print("Error: --stall-threshold-ms must be non-negative", file=sys.stderr)
        return 2

    csv_files = infer_filename_csvs(args.root)
    if not csv_files:
        print("No *_gen*.csv files found under: %s" % args.root, file=sys.stderr)
        return 1

    records = []
    failures = []
    for csv_path in csv_files:
        try:
            records.append(analyze_csv(
                csv_path,
                args.stall_threshold_ms,
                args.pswap,
                args.pgen,
            ))
        except (OSError, ValueError) as error:
            failures.append((csv_path, str(error)))

    os.makedirs(args.output_dir, exist_ok=True)
    individual_path = os.path.join(args.output_dir, "frac_post_individual.csv")
    grouped_path = os.path.join(args.output_dir, "frac_post_by_pswap_pgen.csv")

    write_individual_csv(individual_path, records)
    write_group_csv(grouped_path, records)
    write_console_summary(records)

    print("\nIndividual report: %s" % individual_path)
    print("Grouped report:    %s" % grouped_path)
    print("Files analysed:    %d" % len(records))
    print("Files failed:      %d" % len(failures))

    for csv_path, error in failures:
        print("  FAIL %s: %s" % (csv_path, error), file=sys.stderr)

    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())

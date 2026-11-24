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
import json
import os
import re
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
            filename_lower = filename.lower()
            if "_gen" not in filename_lower or filename_lower.startswith("repeater_"):
                continue
            matches.append(os.path.join(directory, filename))
    return sorted(matches)


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
    mean_success_gap_ms = (
        sum(successful_gaps) / len(successful_gaps) if successful_gaps else None
    )

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


def print_console_table(records, root):
    """Print one formatted row per CSV, keeping frac_post as a table column."""
    headers = [
        "pgen", "pswap", "node", "CSV", "M_att", "n_ok", "frac_ok",
        "frac_post", "M'",
    ]
    rows = []
    for record in records:
        try:
            relative_path = os.path.relpath(record["file"], root)
        except ValueError:
            relative_path = record["file"]
        rows.append([
            "n/a" if record["pgen"] is None else "%.2f" % record["pgen"],
            "n/a" if record["pswap"] is None else "%.2f" % record["pswap"],
            record["node"],
            relative_path,
            str(record["M_attempts"]),
            str(record["n_successes"]),
            "%.3f" % (
                record["n_successes"] / record["M_attempts"]
                if record["M_attempts"] else 0.0
            ),
            "%.3f" % record["frac_post"],
            "%.1f" % record["M_prime"],
        ])

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    border = "-" * (sum(widths) + 3 * len(widths) + 1)

    def format_row(values):
        return "| " + " | ".join(
            value.ljust(widths[index]) for index, value in enumerate(values)
        ) + " |"

    print("\nPost-stall fraction by generation CSV")
    print("frac_post = post-stall successes / successful generations")
    print("M' = (1 - frac_post) * M_att; stall threshold = %.1f ms\n" %
          (records[0]["stall_threshold_ms"] if records else 0.0))
    print(border)
    print(format_row(headers))
    print(border)
    for row in rows:
        print(format_row(row))
    print(border)


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

    records.sort(key=lambda record: (
        record["pswap"] is None,
        record["pswap"] if record["pswap"] is not None else 0.0,
        record["pgen"] is None,
        record["pgen"] if record["pgen"] is not None else 0.0,
        record["node"],
        record["file"],
    ))
    print_console_table(records, os.path.abspath(args.root))
    print("\nCSV files analysed: %d" % len(records))
    print("CSV files skipped:  %d" % len(failures))

    for csv_path, error in failures:
        print("  FAIL %s: %s" % (csv_path, error), file=sys.stderr)

    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())

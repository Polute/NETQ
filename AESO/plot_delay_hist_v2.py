#!/usr/bin/env python3
import argparse
import csv
import statistics
import os
import sys


def load_delays(csv_path):
    delays_us = []
    counts = []
    with open(csv_path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return counts, delays_us
        for row in reader:
            # v2 CSVs use seq,count_idx,delay_ns. Legacy CSVs use count_idx,delay_ns.
            count_value = row.get("count_idx") or row.get("seq")
            delay_value = row.get("delay_ns")
            if count_value is None or delay_value is None:
                continue
            counts.append(int(count_value))
            delays_us.append(float(delay_value) / 1000.0)
    return counts, delays_us


def confirm_overwrite(path):
    if not os.path.exists(path):
        return True
    reply = input(f"overwrite {path}? [y/N]: ").strip().lower()
    return reply == "y"


def robust_outlier_threshold(delays_us, mad_scale):
    median = statistics.median(delays_us)
    deviations = [abs(value - median) for value in delays_us]
    mad = statistics.median(deviations)
    if mad <= 0:
        return median
    return median + mad_scale * mad


def main():
    parser = argparse.ArgumentParser(description="Plot delay histograms from v2 or legacy CSV exports.")
    parser.add_argument("csv", nargs="*", help="CSV files produced by minimal_epr_fast_v2.py --plot or legacy CSV files")
    parser.add_argument("--bins", type=int, default=50)
    parser.add_argument("--csv-dir", default="csv_v2", help="Default CSV directory when no CSV paths are provided.")
    parser.add_argument("--plots-dir", default="plots_v2", help="Base output directory for generated PNGs.")
    parser.add_argument("--prefix", default=None, help="Override output prefix (no extension)")
    parser.add_argument("--last", action="store_true", help="Only plot CSVs without existing outputs.")
    parser.add_argument(
        "--filtered",
        action="store_true",
        help="Also write per-count plots with slow outliers highlighted and removed.",
    )
    parser.add_argument(
        "--filtered-only",
        action="store_true",
        help="Only write the filtered/outlier per-count plots, leaving existing plots untouched.",
    )
    parser.add_argument(
        "--filter-threshold-us",
        type=float,
        default=None,
        help="Delay threshold in us for filtered plots. Defaults to median + mad-scale*MAD.",
    )
    parser.add_argument(
        "--mad-scale",
        type=float,
        default=8.0,
        help="MAD multiplier used for automatic filtered-plot threshold.",
    )
    args = parser.parse_args()
    if args.filtered_only:
        args.filtered = True

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required for plotting.")
        return 1

    csv_paths = args.csv
    if not csv_paths:
        csv_dir = args.csv_dir
        if not os.path.isdir(csv_dir):
            print("csv directory not found")
            return 1
        csv_paths = sorted(
            os.path.join(csv_dir, name)
            for name in os.listdir(csv_dir)
            if name.endswith(".csv")
        )
        if not csv_paths:
            print("no csv files found in current directory")
            return 1

    for csv_path in csv_paths:
        counts, delays_us = load_delays(csv_path)
        if not counts:
            print(f"skip {csv_path} (no data)")
            continue

        os.makedirs(os.path.join(args.plots_dir, "sec"), exist_ok=True)
        os.makedirs(os.path.join(args.plots_dir, "counter"), exist_ok=True)
        if args.filtered:
            os.makedirs(os.path.join(args.plots_dir, "counter_filtered"), exist_ok=True)
            os.makedirs(os.path.join(args.plots_dir, "counter_outliers"), exist_ok=True)
        base = os.path.splitext(os.path.basename(csv_path))[0]
        if args.prefix:
            base = args.prefix
        hist_path = os.path.join(args.plots_dir, "sec", f"{base}.png")
        seq_path = os.path.join(args.plots_dir, "counter", f"{base}_seq.png")
        filtered_path = os.path.join(args.plots_dir, "counter_filtered", f"{base}_seq_filtered.png")
        outliers_path = os.path.join(args.plots_dir, "counter_outliers", f"{base}_seq_outliers.png")

        if args.last:
            if args.filtered_only:
                existing = [filtered_path, outliers_path]
            else:
                existing = [hist_path, seq_path]
                if args.filtered:
                    existing.extend([filtered_path, outliers_path])
            if all(os.path.exists(path) for path in existing):
                print(f"skip {csv_path} (already plotted)")
                continue
        elif not args.filtered_only:
            if not confirm_overwrite(hist_path) or not confirm_overwrite(seq_path):
                print(f"skip {csv_path} (no overwrite)")
                continue

        written = []
        if not args.filtered_only:
            plt.figure(figsize=(8, 4))
            plt.hist(delays_us, bins=args.bins)
            plt.xlabel("delay (us)")
            plt.ylabel("count")
            plt.title(f"delay histogram ({base})")
            plt.tight_layout()
            plt.savefig(hist_path, dpi=150)
            plt.close()

            plt.figure(figsize=(8, 4))
            plt.plot(counts, delays_us, linewidth=0.6)
            plt.xlabel("count")
            plt.ylabel("delay (us)")
            plt.title(f"delay per count ({base})")
            plt.tight_layout()
            plt.savefig(seq_path, dpi=150)
            plt.close()
            written.extend([hist_path, seq_path])

        if args.filtered:
            threshold_us = args.filter_threshold_us
            if threshold_us is None:
                threshold_us = robust_outlier_threshold(delays_us, args.mad_scale)
            kept = [(count, delay) for count, delay in zip(counts, delays_us) if delay <= threshold_us]
            outliers = [(count, delay) for count, delay in zip(counts, delays_us) if delay > threshold_us]
            kept_counts = [count for count, _ in kept]
            kept_delays = [delay for _, delay in kept]
            outlier_counts = [count for count, _ in outliers]
            outlier_delays = [delay for _, delay in outliers]

            plt.figure(figsize=(8, 4))
            plt.plot(kept_counts, kept_delays, linewidth=0.6)
            plt.xlabel("count")
            plt.ylabel("delay (us)")
            plt.title(f"delay per count filtered ({base}, <= {threshold_us:.1f} us)")
            plt.tight_layout()
            plt.savefig(filtered_path, dpi=150)
            plt.close()

            plt.figure(figsize=(8, 4))
            plt.plot(counts, delays_us, linewidth=0.6, label="all samples")
            if outliers:
                plt.scatter(outlier_counts, outlier_delays, s=12, color="red", label="filtered out")
                plt.axhline(threshold_us, color="red", linewidth=0.8, linestyle="--")
            plt.xlabel("count")
            plt.ylabel("delay (us)")
            plt.title(f"delay per count with outliers ({base}, > {threshold_us:.1f} us)")
            if outliers:
                plt.legend()
            plt.tight_layout()
            plt.savefig(outliers_path, dpi=150)
            plt.close()
            written.extend([filtered_path, outliers_path])
            print(f"{base}: filtered {len(outliers)} of {len(delays_us)} samples above {threshold_us:.1f} us")

        print("wrote " + " and ".join(written))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

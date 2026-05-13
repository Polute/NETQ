#!/usr/bin/env python3
import argparse
import csv
import os
import sys


def load_delays(csv_path):
    delays_us = []
    counts = []
    with open(csv_path, "r", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return counts, delays_us
        for row in reader:
            if len(row) < 2:
                continue
            counts.append(int(row[0]))
            delays_us.append(float(row[1]) / 1000.0)
    return counts, delays_us


def confirm_overwrite(path):
    if not os.path.exists(path):
        return True
    reply = input(f"overwrite {path}? [y/N]: ").strip().lower()
    return reply == "y"


def main():
    parser = argparse.ArgumentParser(description="Plot delay histograms from CSV exports.")
    parser.add_argument("csv", nargs="*", help="CSV files produced by minimal_epr_fast.py --plot")
    parser.add_argument("--bins", type=int, default=50)
    parser.add_argument("--prefix", default=None, help="Override output prefix (no extension)")
    parser.add_argument("--last", action="store_true", help="Only plot CSVs without existing outputs.")
    args = parser.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required for plotting.")
        return 1

    csv_paths = args.csv
    if not csv_paths:
        csv_dir = "csv"
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

        os.makedirs(os.path.join("plots", "sec"), exist_ok=True)
        os.makedirs(os.path.join("plots", "counter"), exist_ok=True)
        base = os.path.splitext(os.path.basename(csv_path))[0]
        if args.prefix:
            base = args.prefix
        hist_path = os.path.join("plots", "sec", f"{base}.png")
        seq_path = os.path.join("plots", "counter", f"{base}_seq.png")

        if args.last:
            if os.path.exists(hist_path) or os.path.exists(seq_path):
                print(f"skip {csv_path} (already plotted)")
                continue
        else:
            if not confirm_overwrite(hist_path) or not confirm_overwrite(seq_path):
                print(f"skip {csv_path} (no overwrite)")
                continue

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

        print(f"wrote {hist_path} and {seq_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

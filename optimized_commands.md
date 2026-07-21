# Optimized Commands for AEGO and AESO

Best configuration combinations based on DEOS super-optimized settings (kernel, UDP, Python, CPU).

## AEGO (2 nodes, bidirectional)

### Sender with pgen (probability generation)

```bash
# Terminal 1: Receiver (optimized)
sudo python3 AEGO/minimal_epr_fast.py receiver \
  --listen-host 0.0.0.0 \
  --listen-port 7401 \
  --count 2000 \
  --warmup 50 \
  --cpu 2 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --pace-mode hybrid \
  --spin-margin-us 10.0 \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_aego \
  --json

# Terminal 2: Sender with pgen (optimized)
sudo python3 AEGO/minimal_epr_fast.py sender \
  --receiver-host <RECEIVER_IP> \
  --receiver-port 7401 \
  --count 2000 \
  --warmup 50 \
  --pgen 0.8 \
  --cpu 3 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --pace-mode hybrid \
  --spin-margin-us 10.0 \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_aego \
  --json
```

### Plot Success Analysis (AESO-style)

```bash
# Plot all CSVs from a directory to matching plots directory
python3 AEGO/plot_success_analysis.py csv_aego

# Plot specific CSV file to custom output directory
python3 AEGO/plot_success_analysis.py csv_aego/sender_timing.csv \
  --output-dir plots_aego

# Plot all CSVs from directory to custom output directory
python3 AEGO/plot_success_analysis.py csv_aego \
  --output-dir my_plots

# With filtered plots (outlier removal)
python3 AEGO/plot_success_analysis.py csv_aego \
  --output-dir plots_aego \
  --filtered \
  --mad-scale 8.0

# Force overwrite existing plots
python3 AEGO/plot_success_analysis.py csv_aego \
  --output-dir plots_aego \
  --force

# Only sequential plots
python3 AEGO/plot_success_analysis.py csv_aego \
  --output-dir plots_aego \
  --plot-kind seq
```

## AESO (3 nodes, repeater)

### Repeater with pswap (probability swap)

```bash
# Terminal 1: Repeater (optimized UDP + kernel timestamps)
sudo python3 AESO/minimal_epr_fast.py repeater \
  --listen-host-a 0.0.0.0 \
  --listen-port-a 7401 \
  --listen-host-b 0.0.0.0 \
  --listen-port-b 7402 \
  --count 2000 \
  --cpu 2 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 50 \
  --data-protocol udp \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_aeso \
  --json

# Terminal 2: Client A (optimized)
sudo python3 AESO/minimal_epr_fast.py client \
  --repeater-host <REPEATER_IP> \
  --repeater-port 7401 \
  --count 2000 \
  --warmup 50 \
  --client-id 1 \
  --cpu 3 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 50 \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --data-protocol udp \
  --plot \
  --plot-dir csv_aeso \
  --json

# Terminal 3: Client B (optimized)
sudo python3 AESO/minimal_epr_fast.py client \
  --repeater-host <REPEATER_IP> \
  --repeater-port 7402 \
  --count 2000 \
  --warmup 50 \
  --client-id 2 \
  --cpu 4 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 50 \
  --clock-sync udp \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --clock-sync-kernel-timestamp \
  --kernel-timestamp \
  --data-protocol udp \
  --plot \
  --plot-dir csv_aeso \
  --json
```

### Plot Delay Histogram (AESO-style)

```bash
# Basic plots (histogram + sequential)
python3 AESO/plot_delay_hist.py csv_aeso \
  --bins 50

# With filtered plots (outlier removal)
python3 AESO/plot_delay_hist.py csv_aeso \
  --bins 50 \
  --filtered \
  --mad-scale 8.0

# With custom threshold
python3 AESO/plot_delay_hist.py csv_aeso \
  --bins 50 \
  --filtered \
  --filter-threshold-us 1000

# Only histogram plots
python3 AESO/plot_delay_hist.py csv_aeso \
  --bins 50 \
  --plot-kind hist

# Only sequential plots
python3 AESO/plot_delay_hist.py csv_aeso \
  --bins 50 \
  --plot-kind seq
```

## Key Optimization Parameters

### Common Optimizations (DEOS-style)

- **CPU Affinity**: `--cpu N` - Pin process to specific CPU core
- **RT Priority**: `--rt-priority 50` - Real-time scheduling (requires sudo)
- **Socket Buffers**: `--sock-buf 65536` - Large send/recv buffers
- **Busy Poll**: `--busy-poll-us 0` - Disabled by default (can be enabled if needed)
- **Kernel Timestamps**: `--kernel-timestamp` / `--clock-sync-kernel-timestamp` - Hardware timestamping
- **Pacing Mode**: `--pace-mode hybrid` - Hybrid sleep+spin for low latency
- **Spin Margin**: `--spin-margin-us 10.0` - Final busy-wait window (10us for <500us target)

### AESO-Specific Optimizations

- **UDP Protocol**: `--data-protocol udp` - Faster than TCP for data
- **Clock Sync**: `--clock-sync udp` - PTP-style synchronization
- **Clock Sync Samples**: `--clock-sync-samples 264` - More samples for better accuracy
- **Clock Sync Method**: `--clock-sync-method best-path-median` - Best path filtering
- **Clock Sync Best Ratio**: `--clock-sync-best-ratio 0.5` - Use 50% best samples

### AEGO-Specific Parameters

- **pgen**: `--pgen 0.8` - 80% probability of packet generation (for success/failure analysis)
- **Bidirectional**: TCP-based sender/receiver pattern
- **Kernel Timestamps**: `--kernel-timestamp` - Enable SO_TIMESTAMPNS for hardware timestamping

### Plot Analysis Parameters

- **--force**: Overwrite existing plot files without prompting
- **--filtered**: Generate filtered plots with outlier removal
- **--mad-scale**: MAD multiplier for automatic outlier threshold (default 8.0)
- **--plot-kind**: Limit to "hist", "seq", or "all"
- **--bins**: Number of histogram bins (default 50)

### File Collision Handling

- **Automatic**: CSV, JSON, and plot files automatically append `_1`, `_2`, etc. if file exists
- **Force Override**: Use `--force` to overwrite existing files without collision handling

## Directory Structure

```
NETQ/
├── AEGO/
│   ├── csv_aego/           # CSV outputs
│   ├── json_aego/          # JSON metadata
│   └── plots_aego/         # Plots
│       ├── sec/            # Histograms
│       ├── counter/        # Sequential plots
│       ├── sec_filtered/   # Filtered histograms
│       ├── counter_filtered/ # Filtered sequential
│       └── counter_outliers/ # Outlier plots
├── AESO/
│   ├── csv_aeso/           # CSV outputs
│   ├── json_aeso/          # JSON metadata
│   └── plots_aeso/         # Plots
│       ├── sec/            # Histograms
│       ├── counter/        # Sequential plots
│       ├── sec_filtered/   # Filtered histograms
│       ├── counter_filtered/ # Filtered sequential
│       └── counter_outliers/ # Outlier plots
```

## Notes

- Replace `<RECEIVER_IP>` and `<REPEATER_IP>` with actual IP addresses
- Use `sudo` for RT priority and kernel timestamps
- Adjust CPU cores based on your system (avoid core 0 for system tasks)
- Increase `--count` for longer runs (e.g., 10000)
- Adjust `--pgen` for different success/failure ratios in AEGO
- Use `--filtered` plots to identify and remove outliers
- The `--mad-scale 8.0` is a good default for outlier detection

# DEOS Fast UDP Prototype

DEOS extends the AESO notification idea to two sequential entanglement-swapping operations over four processes:

- Alice: endpoint client.
- R1: first repeater, swaps Alice-R1 with R1-R2.
- R2: second repeater, receives R1's intermediate result and swaps Alice-R2 with R2-Bob.
- Bob: endpoint client.

The implementation is intentionally self-contained: `deos_fast.py` does not import any project-local Python file.

## What It Sends

R1 starts with two Werner links:

- `w12`: Alice-R1.
- `w34`: R1-R2.

For each count, R1 computes:

```text
w14 = w12 * w34
```

Then R1 sends:

- R1 -> Alice: R1 timestamp, R2 identity, R1 correction bits, `w14`.
- R1 -> R2: R1 timestamp, Alice identity, R1 correction bits, `w14`.

After R2 receives the intermediate message, R2 starts the second swapping with:

- `w14`: Alice-R2 intermediate Werner value.
- `w56`: R2-Bob Werner value.

For each count, R2 computes:

```text
w_final = w14 * w56
bits_final = bits_r1 XOR bits_r2_local
```

The XOR is the accumulated Pauli correction: bit 0 commutes with bit 0, and bit 1 commutes with bit 1.

Then R2 sends:

- R2 -> Alice: final timestamp/correction information, Bob identity, `w_final`.
- R2 -> Bob: final timestamp/correction information, Alice identity, `w_final`.

## Timing Model

The first R1 timestamp is the global reference timestamp for the experiment.

The main endpoint histograms are:

- `R1 initial -> R2 -> Alice`: Alice final receive time corrected to R1 clock minus the first R1 timestamp.
- `R1 initial -> R2 -> Bob`: Bob final receive time corrected to R1 clock minus the first R1 timestamp.

R2 also records the intermediate R1 -> R2 delay.

All data transport is UDP. Clock synchronization is optional and, when enabled,
uses a UDP PTP-like exchange with kernel receive timestamps when available
through `SO_TIMESTAMPNS`.

## Default Optimized Settings

These are already the defaults in `deos_fast.py`:

- 2000 counts.
- 50 warmup counts on Alice/Bob final statistics.
- `rt_priority = 50`.
- UDP data transport.
- Clock synchronization disabled unless `--clock-sync` is passed.
- Kernel receive timestamps for data.
- Kernel receive timestamps for clock sync when `--clock-sync` is enabled.
- 264 clock-sync samples when `--clock-sync` is enabled.
- Best-path median offset estimator when `--clock-sync` is enabled.
- 5 percent clock-sync warmup when `--clock-sync` is enabled.
- `sock_buf = 65536`.
- `busy_poll_us = 50`.
- R1 pacing: 50 us between counts.
- R1 pacing mode: spin.
- JSON output enabled when `--plot` is used.

Use `--rt-priority 0` only for non-root local smoke tests. For real measurements, run with `sudo` so the default real-time priority can be applied.

To enable clock synchronization in any command, add:

```bash
--clock-sync
```

The default clock-sync settings then use UDP, kernel receive timestamps, 264
samples, 5 percent sync warmup, and the best-path median estimator.

## Four-Node Commands

Replace `<R1_HOST>` and `<R2_HOST>` with the reachable hostnames or addresses of R1 and R2.

Recommended role mapping:

- R1: first repeater.
- R2: second repeater.
- Alice: endpoint connected to R1 and R2.
- Bob: endpoint connected to R1 for clock reference and to R2 for final data.

Start R1 first. Then start Alice, R2, and Bob in their own terminals.

### R1

```bash
cd ~/DEOS
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 deos_fast.py r1 \
  --listen-host-a 0.0.0.0 \
  --listen-host-r2 0.0.0.0 \
  --listen-host-bob-sync 0.0.0.0 \
  --w12 1 \
  --w34 1 \
  --quiet \
  --plot \
  --plot-dir csv_deos_udp_kernel_spin \
  --cpu 1
```

### Alice

```bash
cd ~/DEOS
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 deos_fast.py alice \
  --r1-host <R1_HOST> \
  --r2-host <R2_HOST> \
  --quiet \
  --plot \
  --plot-dir csv_deos_udp_kernel_spin \
  --cpu 1
```

### R2

```bash
cd ~/DEOS
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 deos_fast.py r2 \
  --r1-host <R1_HOST> \
  --listen-host-a 0.0.0.0 \
  --listen-host-b 0.0.0.0 \
  --w56 1 \
  --quiet \
  --plot \
  --plot-dir csv_deos_udp_kernel_spin \
  --cpu 1
```

### Bob

```bash
cd ~/DEOS
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 deos_fast.py bob \
  --r1-sync-host <R1_HOST> \
  --r2-host <R2_HOST> \
  --quiet \
  --plot \
  --plot-dir csv_deos_udp_kernel_spin \
  --cpu 1
```

## Default Ports

R1:

- Alice control/data/sync: `7601`.
- R2 control/data/sync: `7602`.
- Bob clock-sync only: `7603`.

R2:

- Alice control/data/sync: `7611`.
- Bob control/data/sync: `7612`.

Override them with `--listen-port-*`, `--r1-port`, `--r1-sync-port`, and `--r2-port` if needed.

## Local Smoke Test

For a local test without real-time scheduling:

### Terminal 1: R1

```bash
cd ~/DEOS
PYTHONDONTWRITEBYTECODE=1 python3 deos_fast.py r1 \
  --listen-host-a 127.0.0.1 \
  --listen-host-r2 127.0.0.1 \
  --listen-host-bob-sync 127.0.0.1 \
  --count 20 \
  --rt-priority 0 \
  --plot \
  --plot-dir csv_deos_smoke \
  --json-dir json_deos_smoke \
  --quiet
```

### Terminal 2: Alice

```bash
cd ~/DEOS
PYTHONDONTWRITEBYTECODE=1 python3 deos_fast.py alice \
  --r1-host 127.0.0.1 \
  --r2-host 127.0.0.1 \
  --count 20 \
  --warmup 0 \
  --rt-priority 0 \
  --plot \
  --plot-dir csv_deos_smoke \
  --json-dir json_deos_smoke \
  --quiet
```

### Terminal 3: R2

```bash
cd ~/DEOS
PYTHONDONTWRITEBYTECODE=1 python3 deos_fast.py r2 \
  --r1-host 127.0.0.1 \
  --listen-host-a 127.0.0.1 \
  --listen-host-b 127.0.0.1 \
  --count 20 \
  --rt-priority 0 \
  --plot \
  --plot-dir csv_deos_smoke \
  --json-dir json_deos_smoke \
  --quiet
```

### Terminal 4: Bob

```bash
cd ~/DEOS
PYTHONDONTWRITEBYTECODE=1 python3 deos_fast.py bob \
  --r1-sync-host 127.0.0.1 \
  --r2-host 127.0.0.1 \
  --count 20 \
  --warmup 0 \
  --rt-priority 0 \
  --plot \
  --plot-dir csv_deos_smoke \
  --json-dir json_deos_smoke \
  --quiet
```

## Outputs

With `--plot`, each process writes CSV files grouped by role. JSON metadata is
also written by default, using the same role subdirectories.

Typical CSV tree:

```text
csv_deos_smoke/
  r2/
    01_r1_to_r2_aeso.csv
  alice/
    01_r1_to_alice_aeso.csv
    02_r2_to_alice_aeso.csv
    03_r1_initial_to_r2_alice_sum.csv
  bob/
    01_r2_to_bob_aeso.csv
    02_r1_initial_to_r2_bob_sum.csv
```

When `--clock-sync` is enabled, each synchronized role also writes
`clock_sync_*.csv` files in its own role directory.

Chronology:

- `01_r1_to_alice_aeso.csv`: first AESO communication, R1 -> Alice.
- `01_r1_to_r2_aeso.csv`: first AESO communication, R1 -> R2.
- `02_r2_to_alice_aeso.csv`: second AESO communication, R2 -> Alice.
- `01_r2_to_bob_aeso.csv`: second AESO communication, R2 -> Bob.
- `03_r1_initial_to_r2_alice_sum.csv`: sum from R1 initial timestamp to Alice final receive through R2.
- `02_r1_initial_to_r2_bob_sum.csv`: sum from R1 initial timestamp to Bob final receive through R2.

The two `*_sum.csv` files are the main files for evaluating the full classical
notification time and the final Werner value. R1 itself writes JSON metadata
with the initial state and arguments, but no separate CSV is needed for the
origin timestamp because it is already embedded in the link and sum CSVs.

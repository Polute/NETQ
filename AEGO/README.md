# AEGO - Fast EPR-over-TCP with Clock Synchronization

This repo contains the fast EPR-over-TCP implementation focused on latency and consistent measurement, with support for multiple clock synchronization strategies.

## Clock Synchronization Options

AEGO supports two clock synchronization modes:

1. **No sync** - Default mode, no clock synchronization
2. **Native UDP sync** - Custom UDP-based clock synchronization (no Linux PTP required)
3. **Linux PTP sync** - Uses Linux PTP (ptp4l) via separate daemon for hardware-assisted synchronization

## Complete Command Examples

### 1. Without Clock Synchronization (Default)

**Receiver:**
```bash
sudo python3 AEGO/minimal_epr_fast.py receiver \
  --listen-host 0.0.0.0 \
  --listen-port 7401 \
  --count 2000 \
  --warmup 50 \
  --accept-timeout 30.0 \
  --cpu 3 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --pgen 0.8 \
  --plot \
  --plot-dir csv_aego_3 \
  --json \
  --quiet
```

**Sender:**
```bash
sudo python3 AEGO/minimal_epr_fast.py sender \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --count 2000 \
  --warmup 50 \
  --connect-timeout 10.0 \
  --detect-timeout 30.0 \
  --detect-interval 0.05 \
  --cpu 2 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_aego_3 \
  --json \
  --quiet
```

### 2. With Native UDP Clock Synchronization

**Receiver (acts as UDP clock sync server):**
```bash
sudo python3 AEGO/minimal_epr_fast.py receiver \
  --listen-host 0.0.0.0 \
  --listen-port 7401 \
  --count 2000 \
  --warmup 50 \
  --accept-timeout 30.0 \
  --cpu 3 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --pgen 0.8 \
  --clock-sync \
  --clock-sync-port 7501 \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --clock-sync-kernel-timestamp \
  --plot \
  --plot-dir csv_aego_3 \
  --json \
  --quiet
```

**Sender (acts as UDP clock sync client):**
```bash
sudo python3 AEGO/minimal_epr_fast.py sender \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --count 2000 \
  --warmup 50 \
  --connect-timeout 10.0 \
  --detect-timeout 30.0 \
  --detect-interval 0.05 \
  --cpu 2 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --kernel-timestamp \
  --clock-sync \
  --clock-sync-port 7501 \
  --clock-sync-samples 264 \
  --clock-sync-warmup 10 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --clock-sync-kernel-timestamp \
  --plot \
  --plot-dir csv_aego_3 \
  --json \
  --quiet
```

### 3. With Linux PTP Clock Synchronization (Separate Daemon)

**Step 1: Start PTP Master (Grandmaster) on separate CPU:**

```bash
sudo taskset -c 4 python3 AEGO/sync_ptp_daemon.py \
  --master \
  --interface enp6s18 \
  --target-offset 10000
```

**Step 2: Start PTP Slave on separate CPU:**

```bash
sudo taskset -c 5 python3 AEGO/sync_ptp_daemon.py \
  --slave 192.168.1.100 \
  --interface enp6s18 \
  --target-offset 10000
```

**Step 3: Run AEGO (sender/receiver will read offset from /tmp/ptp_status.json):**

**Receiver:**
```bash
sudo python3 AEGO/minimal_epr_fast.py receiver \
  --listen-host 0.0.0.0 \
  --listen-port 7401 \
  --count 2000 \
  --warmup 50 \
  --accept-timeout 30.0 \
  --cpu 3 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --pgen 0.8 \
  --plot \
  --plot-dir csv_aego_3 \
  --json \
  --quiet
```

**Sender:**
```bash
sudo python3 AEGO/minimal_epr_fast.py sender \
  --receiver-host 127.0.0.1 \
  --receiver-port 7401 \
  --count 2000 \
  --warmup 50 \
  --connect-timeout 10.0 \
  --detect-timeout 30.0 \
  --detect-interval 0.05 \
  --cpu 2 \
  --rt-priority 50 \
  --sock-buf 65536 \
  --busy-poll-us 0 \
  --kernel-timestamp \
  --plot \
  --plot-dir csv_aego_3 \
  --json \
  --quiet
```

## Important Notes

- **Linux PTP flag `-S`**: The PTP daemon uses the `-S` flag with ptp4l to **only report offset without changing the system clock**. This preserves precision when requesting timestamps from the kernel.
- **Separate PTP daemon**: Linux PTP runs as a separate process (`sync_ptp_daemon.py`) on a dedicated CPU to maintain synchronization in the background while AEGO runs. AEGO reads the offset from `/tmp/ptp_status.json`.
- **pgen**: The `--pgen` argument is only used by the receiver (Bob) to decide success/failure. The sender always sends all packets.
- **Clock sync options**: Native UDP sync (`--clock-sync`) is mutually exclusive with PTP daemon (which runs separately).
- **sudo required**: For RT priority (`--rt-priority`) and PTP operations, run with sudo.
- **Network interface**: Update `--interface` in PTP daemon to match your network interface (e.g., `enp6s18`, `eth0`).
- **CPU pinning**: Use `taskset` to pin the PTP daemon to a dedicated CPU for best performance.

## Plotting Results

After running with `--plot`, generate plots:

```bash
python3 AEGO/plot_success_analysis.py csv_aego_3
```

This will generate RTT histograms, sequential plots, and inter-success analysis in the `plots_aego_2` directory.

## Timing Arrows Mapping

```
  Sender                             Receiver
  |                                   |
  | ts_emit_ns (unix)                 |
  |-----------------┐                 |
  |                 └---------------->|  sender_to_receiver (ts_recv_ns (receiver time))
  |                                   |  (local state update)
  |                      ts_recv_ns   |
  |                 ┌-----------------|  receiver_to_ack_send
  |<----------------┘                 |  
  |<----- total_round_trip_perf ----->|

  
  Sender                             Receiver
  |                                   |
  | ts_emit_ns (unix)                 |
  |-----------------┐                 |
  |                 └---------------->|  sender_to_receiver (ts_recv_ns (receiver time))
  |                                   |  (local state update)
  |                      ts_recv_ns   |
  |                 ┌-----------------|  receiver_to_ack_send
  |<-------- total_receiver_view --------->|
```

## Network Configuration

### Remote Proxmox VM through SSH Jump Host

**Receiver on PC, sender on VM:**

First, open the reverse SSH tunnel from the PC:

```bash
ssh -J <jump-user>@<jump-host> -o Compression=no -R 7401:127.0.0.1:7401 <vm-user>@<vm-private-ip>
```

On the PC, run the receiver:

```bash
sudo python3 AEGO/minimal_epr_fast.py receiver
```

On the VM, run the sender:

```bash
sudo python3 AEGO/minimal_epr_fast.py sender --receiver-host 127.0.0.1 --receiver-port 7401
```

**Sender on PC, receiver on VM:**

First, open the local SSH tunnel from the PC:

```bash
ssh -J <jump-user>@<jump-host> -L 7402:127.0.0.1:7402 <vm-user>@<vm-private-ip>
```

On the PC, run the sender:

```bash
sudo python3 AEGO/minimal_epr_fast.py sender --receiver-host 127.0.0.1 --receiver-port 7402
```

On the VM, run the receiver:

```bash
sudo python3 AEGO/minimal_epr_fast.py receiver --listen-host 127.0.0.1 --listen-port 7402
```

### Direct Ethernet Cable

Use this mode when the PC and the Proxmox VM are connected through the dedicated Ethernet link.

**Receiver on VM, sender on PC:**

On the VM, run the receiver:

```bash
sudo python3 AEGO/minimal_epr_fast.py receiver --listen-host 0.0.0.0 --listen-port 7402
```

On the PC, run the sender:

```bash
sudo python3 AEGO/minimal_epr_fast.py sender --receiver-host 10.10.10.2 --receiver-port 7402
```

**Receiver on PC, sender on VM:**

On the PC, run the receiver:

```bash
sudo python3 AEGO/minimal_epr_fast.py receiver --listen-host 0.0.0.0 --listen-port 7401
```

On the VM, run the sender:

```bash
sudo python3 AEGO/minimal_epr_fast.py sender --receiver-host 10.10.10.1 --receiver-port 7401
```

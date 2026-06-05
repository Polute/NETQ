# AESO Docker Test Environment

Updated on 2026-06-03.

This README is the current baseline for running `minimal_epr_fast.py` and the C
port inside an Ubuntu 22.04 style Docker environment with Python 3.10.

## Build The Image

Run from `/home/giicc/NETQ`:

```bash
docker build -f Dockerfile.ubuntu2204-py310 -t netq-ubuntu2204-py310 .
```

The image uses Ubuntu 22.04 packages, so `python3` is the Ubuntu 22.04 Python
3.10 line. It also installs `iproute2`, `procps`, `htop`, `sudo` and basic
debug tools.

## Open Three Terminals

Open three separate terminals. Give each terminal one CPU, matching the CPU
used later by the command running inside that terminal.

Terminal 1, repeater on CPU 3:

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="3" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/AESO \
  netq-ubuntu2204-py310
```

Terminal 2, client 1 on CPU 5:

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="5" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/AESO \
  netq-ubuntu2204-py310
```

Terminal 3, client 2 on CPU 7:

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="7" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/AESO \
  netq-ubuntu2204-py310
```

## Python UDP Baseline

This is the recommended local Docker baseline. Start the repeater first, then
the two clients.

Repeater:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater \
  --listen-host-a 127.0.0.1 \
  --listen-port-a 7401 \
  --listen-host-b 127.0.0.1 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol udp \
  --plot \
  --plot-dir csv_docker_udp_kernel_ts_paced \
  --accept-timeout 120 \
  --cpu 3 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --shared-send-timestamp \
  --count-interval 0.00005 \
  --pace-mode sleep
```

Client 1:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --plot \
  --plot-dir csv_docker_udp_kernel_ts_paced \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 5 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

Client 2:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --plot \
  --plot-dir csv_docker_udp_kernel_ts_paced \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 7 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

CSV files go to `csv_docker_udp_kernel_ts_paced/`. JSON files are enabled by
default and go to `json_docker_udp_kernel_ts_paced/`.

## Python TCP Baseline

Use the same structure, changing only the transport and output folder. TCP does
not use `--kernel-timestamp`.

Repeater:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater \
  --listen-host-a 127.0.0.1 \
  --listen-port-a 7401 \
  --listen-host-b 127.0.0.1 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol tcp \
  --plot \
  --plot-dir csv_docker_tcp_paced \
  --accept-timeout 120 \
  --cpu 3 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --shared-send-timestamp \
  --count-interval 0.00005 \
  --pace-mode sleep
```

Client 1:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --plot \
  --plot-dir csv_docker_tcp_paced \
  --data-protocol tcp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 5 \
  --sock-buf 65536 \
  --busy-poll-us 25
```

Client 2:

```bash
sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --plot \
  --plot-dir csv_docker_tcp_paced \
  --data-protocol tcp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 7 \
  --sock-buf 65536 \
  --busy-poll-us 25
```

## Build And Run The C Port

Inside the Docker container:

```bash
gcc -O3 -Wall -Wextra -pthread -o c_code/minimal_epr_fast_c c_code/minimal_epr_fast.c -lm
```

Do this build inside the Docker container, not on the host. If the binary was
compiled on a newer host, Ubuntu 22.04 may fail with an error like
`GLIBC_2.38 not found`.

C UDP repeater:

```bash
sudo ./c_code/minimal_epr_fast_c repeater \
  --listen-host-a 127.0.0.1 \
  --listen-port-a 7401 \
  --listen-host-b 127.0.0.1 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --data-protocol udp \
  --plot \
  --plot-dir csv_docker_c_udp_kernel_ts_paced \
  --accept-timeout 120 \
  --cpu 3 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --shared-send-timestamp \
  --count-interval 0.00005 \
  --pace-mode sleep
```

C UDP client 1:

```bash
sudo ./c_code/minimal_epr_fast_c client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --plot \
  --plot-dir csv_docker_c_udp_kernel_ts_paced \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 5 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

C UDP client 2:

```bash
sudo ./c_code/minimal_epr_fast_c client \
  --repeater-host 127.0.0.1 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --plot \
  --plot-dir csv_docker_c_udp_kernel_ts_paced \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 7 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

The C port now accepts the same key flags as the Python baseline:

- `--shared-send-timestamp`
- `--pace-mode sleep|spin|hybrid`
- `--spin-margin-us`
- `--kernel-timestamp`
- `--json`, `--no-json`, `--json-dir`
- `--sock-buf 65536` default on both repeater and client
- `--clock-sync-method mean|median|best-path-median`
- `--clock-sync-best-ratio`

It also keeps the extra C-only diagnostic/control option:

- `--send-mode burst|paced|ack`

Use `ack` only as a control-flow diagnostic. It changes the experiment because
each message waits for client acknowledgement.

## Current Help Summary

Python repeater:

```bash
python3 minimal_epr_fast.py repeater -h
```

Important current options:

```text
--shared-send-timestamp
--count-interval COUNT_INTERVAL
--pace-mode {sleep,spin,hybrid}
--spin-margin-us SPIN_MARGIN_US
--plot --plot-prefix PLOT_PREFIX --plot-dir PLOT_DIR
--json / --no-json --json-dir JSON_DIR
--diag
--clock-sync --clock-sync-samples CLOCK_SYNC_SAMPLES
--data-protocol {udp,tcp}
--udp-ready-timeout UDP_READY_TIMEOUT
```

Python client:

```bash
python3 minimal_epr_fast.py client -h
```

Important current options:

```text
--plot --plot-prefix PLOT_PREFIX --plot-dir PLOT_DIR
--json / --no-json --json-dir JSON_DIR
--clock-sync --clock-sync-samples CLOCK_SYNC_SAMPLES --clock-sync-warmup CLOCK_SYNC_WARMUP
--clock-sync-method {mean,median,best-path-median} --clock-sync-best-ratio CLOCK_SYNC_BEST_RATIO
--clock-offset-ns CLOCK_OFFSET_NS
--center-delay
--data-protocol {udp,tcp}
--udp-idle-timeout UDP_IDLE_TIMEOUT
--kernel-timestamp
```

C:

```bash
./c_code/minimal_epr_fast_c -h
```

Current C help includes:

```text
Common: --count --quiet --plot --plot-dir --json/--no-json --json-dir
Common: --cpu --rt-priority --sock-buf --busy-poll-us
Common: --data-protocol udp|tcp --clock-sync --clock-sync-samples --clock-sync-warmup
Common: --clock-sync-method mean|median|best-path-median --clock-sync-best-ratio
Repeater: --shared-send-timestamp --count-interval --pace-mode --spin-margin-us
Repeater: --send-mode burst|paced|ack --udp-ready-timeout
Client: --clock-offset-ns --center-delay --udp-idle-timeout --kernel-timestamp
```

Clock sync warmup defaults to `floor(0.05 * --clock-sync-samples)`, so `--clock-sync-samples 264` discards the first 13 sync samples. The default offset estimator is `best-path-median` with `--clock-sync-best-ratio 0.5`: after warmup, it keeps the 50% of sync samples with lowest path delay and uses the median offset from that subset. The CSV/JSON still store mean, median, best-path median, path-delay stats and `sync_quality`.

## Three Real Nodes

Use this when the real machines are:

```text
192.168.0.226  repeater
192.168.0.223  client 1
192.168.0.227  client 2
```

All three commands use CPU 1 because those VMs only expose two CPUs.

### Python UDP With Clock Sync

Run first on `.226`:

```bash
cd ~/AESO_opt

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py repeater \
  --listen-host-a 0.0.0.0 \
  --listen-port-a 7401 \
  --listen-host-b 0.0.0.0 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --clock-sync \
  --clock-sync-samples 264 \
  --data-protocol udp \
  --plot \
  --plot-dir csv_3nodes_py_udp_sync264 \
  --accept-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --shared-send-timestamp \
  --count-interval 0.00005 \
  --pace-mode sleep
```

Run on `.223`:

```bash
cd ~/AESO_opt

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 192.168.0.226 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --clock-sync \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --plot \
  --plot-dir csv_3nodes_py_udp_sync264 \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

Run on `.227`:

```bash
cd ~/AESO_opt

sudo env PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 minimal_epr_fast.py client \
  --repeater-host 192.168.0.226 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --clock-sync \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --plot \
  --plot-dir csv_3nodes_py_udp_sync264 \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

### C UDP With Clock Sync

Compile the C binary in a compatible Ubuntu 22.04 environment, or copy a binary
compiled inside the Docker image to each machine.

Run first on `.226`:

```bash
cd ~/AESO_opt

sudo ./c_code/minimal_epr_fast_c repeater \
  --listen-host-a 0.0.0.0 \
  --listen-port-a 7401 \
  --listen-host-b 0.0.0.0 \
  --listen-port-b 7402 \
  --werner-ar 1 \
  --werner-br 1 \
  --quiet \
  --clock-sync \
  --clock-sync-samples 264 \
  --data-protocol udp \
  --plot \
  --plot-dir csv_3nodes_c_udp_sync264 \
  --accept-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --shared-send-timestamp \
  --count-interval 0.00005 \
  --pace-mode sleep
```

Run on `.223`:

```bash
cd ~/AESO_opt

sudo ./c_code/minimal_epr_fast_c client \
  --repeater-host 192.168.0.226 \
  --repeater-port 7401 \
  --client-id 1 \
  --quiet \
  --clock-sync \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --plot \
  --plot-dir csv_3nodes_c_udp_sync264 \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

Run on `.227`:

```bash
cd ~/AESO_opt

sudo ./c_code/minimal_epr_fast_c client \
  --repeater-host 192.168.0.226 \
  --repeater-port 7402 \
  --client-id 2 \
  --quiet \
  --clock-sync \
  --clock-sync-samples 264 \
  --clock-sync-method best-path-median \
  --clock-sync-best-ratio 0.5 \
  --plot \
  --plot-dir csv_3nodes_c_udp_sync264 \
  --data-protocol udp \
  --connect-timeout 120 \
  --detect-timeout 120 \
  --cpu 1 \
  --sock-buf 65536 \
  --busy-poll-us 25 \
  --kernel-timestamp
```

## Plotting

Generate plots from a CSV folder:

```bash
python3 plot_delay_hist.py csv_docker_udp_kernel_ts_paced
```

If the plot script expects a different argument shape, check:

```bash
python3 plot_delay_hist.py -h
```

## Notes

- Do not use `--diag` in final performance runs. It adds extra timestamp calls.
- Use `--kernel-timestamp` only with UDP clients.
- Docker `--network host` avoids Docker NAT and keeps the local network path
  closer to the real host path.
- WSL can introduce jitter even when CPU usage looks low. Treat Docker/native
  Linux results as more representative than WSL terminal results.
- If files are created as root, the Python and C code try to chown CSV/JSON
  outputs back to the sudo user when `SUDO_UID`/`SUDO_GID` are available.

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

Open three separate terminals with the same command:

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="3,5,7" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/AESO \
  netq-ubuntu2204-py310
```

Use one terminal as repeater, one as client 1, and one as client 2.

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
--clock-sync --clock-sync-samples CLOCK_SYNC_SAMPLES
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
Common: --data-protocol udp|tcp --clock-sync --clock-sync-samples
Repeater: --shared-send-timestamp --count-interval --pace-mode --spin-margin-us
Repeater: --send-mode burst|paced|ack --udp-ready-timeout
Client: --clock-offset-ns --center-delay --udp-idle-timeout --kernel-timestamp
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

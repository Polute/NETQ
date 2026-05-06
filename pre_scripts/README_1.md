# Fast EPR Benchmark

## Localhost (recommended fast mode)

Receiver:

```bash
sudo taskset -c 3 python pre_scripts/minimal_epr_receiver_fast.py --listen-port 7401 --count 3000 --warmup 30 --rt-priority 50 --busy-poll-us 25 --quiet
```

Sender:

```bash
sudo taskset -c 2 python pre_scripts/minimal_epr_sender_fast.py --receiver-host 127.0.0.1 --receiver-port 7401 --count 3000 --warmup 30 --rt-priority 50 --busy-poll-us 25 --quiet --show-arrows
```

Timing arrows mapping (fast sender output):

```
Sender                             Receiver
  |                                   |
  |  ts_emit_ns (unix)                |
  |---------------------------------->|  ts_recv_ns (receiver time)
  |                                   |  (local state update)
  |<----------------------------------|  ack ts (receiver send time)
  |                                   |
  |<-------- total_round_trip_perf --------->|

sender_to_receiver     = ts_recv_ns - ts_emit_ns
sender_local_send_call = sendall() duration on sender
receiver_to_sender_back= total_round_trip_perf - sender_to_receiver
```

## Remote VM over SSH jump host

```bash
python pre_scripts/run_fast_ssh.py \
  --jump-user JUMP_USER \
  --jump-host PUBLIC_IP \
  --vm-user VM_USER \
  --vm-host VM_PRIVATE_IP \
  --vm-workdir ~/NETQ \
  --receiver-bind-ip VM_PRIVATE_IP \
  --receiver-port 7401 \
  --count 3000 \
  --warmup 30 \
  --busy-poll-us 25 \
  --receiver-cpu 3 \
  --sender-cpu 2 \
  --rt-priority 50
```

Notes:

- Passwords are not stored in code.
- `ssh` and `sudo` prompt interactively when needed.

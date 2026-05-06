Fast unified (minimal_epr_fast.py)

Receiver (with sudo for RT):

    sudo python minimal_epr_fast.py receiver \
      --listen-host 0.0.0.0 \
      --listen-port 7401 \
      --count 1000 \
      --warmup 50 \
      --accept-timeout 30.0 \
      --cpu 3 \
      --rt-priority 50 \
      --sock-buf 0 \
      --busy-poll-us 25 \
      --werner-min 0.2 \
      --t1-ns 1000000.0 \
      --quiet

Sender (with sudo for RT):

    sudo python minimal_epr_fast.py sender \
      --receiver-host 127.0.0.1 \
      --receiver-port 7401 \
      --count 1000 \
      --warmup 50 \
      --connect-timeout 10.0 \
      --detect-timeout 30.0 \
      --detect-interval 0.05 \
      --cpu 2 \
      --rt-priority 50 \
      --sock-buf 0 \
      --busy-poll-us 25 \
      --show-arrows \
      --werner-min 0.2 \
      --t1-ns 1000000.0 \
      --quiet

Note

- If you do not have RT permissions, remove --rt-priority or run without sudo.


Timing arrows mapping (fast sender output):

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

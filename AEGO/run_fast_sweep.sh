#!/bin/bash
set -euo pipefail

# Simple sweep runner for minimal_epr_fast.py with repeats and summary stats.
# Adjust lists below as needed.

SUDO=${SUDO:-sudo}
PYTHON=${PYTHON:-python}
WORKDIR=${WORKDIR:-/home/giicc/NETQ}

RECEIVER_HOST=${RECEIVER_HOST:-0.0.0.0}
RECEIVER_PORT=${RECEIVER_PORT:-7401}
RECEIVER_CPU=${RECEIVER_CPU:-3}
SENDER_CPU=${SENDER_CPU:-2}

COUNT_LIST=(1000 3000)
WARMUP=${WARMUP:-50}
DETECT_INTERVAL_LIST=(0.01 0.05 0.1)
BUSY_POLL_LIST=(0 25 50)
RT_PRIORITY_LIST=(50)
SOCK_BUF_LIST=(0 4096 65536)

ACCEPT_TIMEOUT=${ACCEPT_TIMEOUT:-30.0}
CONNECT_TIMEOUT=${CONNECT_TIMEOUT:-10.0}
DETECT_TIMEOUT=${DETECT_TIMEOUT:-30.0}

REPEATS=${REPEATS:-5}
OUTFILE_RAW=${OUTFILE_RAW:-/tmp/fast_sweep_results_raw.tsv}
OUTFILE_SUM=${OUTFILE_SUM:-/tmp/fast_sweep_results_summary.tsv}

cd "$WORKDIR"

run_case() {
  local count=$1
  local detect_interval=$2
  local busy_poll=$3
  local rt_prio=$4
  local sock_buf=$5

  local recv_cmd=("$PYTHON" minimal_epr_fast.py receiver
    --listen-host "$RECEIVER_HOST"
    --listen-port "$RECEIVER_PORT"
    --count "$count"
    --warmup "$WARMUP"
    --accept-timeout "$ACCEPT_TIMEOUT"
    --cpu "$RECEIVER_CPU"
    --sock-buf "$sock_buf"
    --busy-poll-us "$busy_poll"
    --werner-min 0.2
    --t1-ns 1000000.0
    --quiet
  )

  local send_cmd=("$PYTHON" minimal_epr_fast.py sender
    --receiver-host 127.0.0.1
    --receiver-port "$RECEIVER_PORT"
    --count "$count"
    --warmup "$WARMUP"
    --connect-timeout "$CONNECT_TIMEOUT"
    --detect-timeout "$DETECT_TIMEOUT"
    --detect-interval "$detect_interval"
    --cpu "$SENDER_CPU"
    --sock-buf "$sock_buf"
    --busy-poll-us "$busy_poll"
    --werner-min 0.2
    --t1-ns 1000000.0
    --quiet
  )

  if [[ -n "$rt_prio" ]]; then
    recv_cmd+=(--rt-priority "$rt_prio")
    send_cmd+=(--rt-priority "$rt_prio")
  fi

  local recv_log
  local send_log
  recv_log=$(mktemp)
  send_log=$(mktemp)

  local rep
  for rep in $(seq 1 "$REPEATS"); do
    $SUDO "${recv_cmd[@]}" >"$recv_log" 2>&1 &
    local recv_pid=$!

    sleep 0.2

    if ! $SUDO "${send_cmd[@]}" >"$send_log" 2>&1; then
      wait "$recv_pid" || true
      echo "run_failed\tcount=$count\tdetect_interval=$detect_interval\tbusy_poll=$busy_poll\trt_prio=$rt_prio\tsock_buf=$sock_buf" >> "$OUTFILE_RAW"
      continue
    fi

    wait "$recv_pid" || true

    local p50_rtt
    p50_rtt=$(awk 'BEGIN{flag=0} /^sender_p50/{flag=1; next} flag && /total_round_trip_perf/{print $2; exit}' "$send_log")
    if [[ -z "$p50_rtt" ]]; then
      p50_rtt=-1
    fi

    printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
      "$count" "$detect_interval" "$busy_poll" "$rt_prio" "$sock_buf" "$p50_rtt" >> "$OUTFILE_RAW"
  done

  rm -f "$recv_log" "$send_log"
}

printf "count\tdetect_interval\tbusy_poll_us\trt_priority\tsock_buf\tp50_rtt_ns\n" > "$OUTFILE_RAW"

for count in "${COUNT_LIST[@]}"; do
  for detect_interval in "${DETECT_INTERVAL_LIST[@]}"; do
    for busy_poll in "${BUSY_POLL_LIST[@]}"; do
      for rt_prio in "${RT_PRIORITY_LIST[@]}"; do
        for sock_buf in "${SOCK_BUF_LIST[@]}"; do
          run_case "$count" "$detect_interval" "$busy_poll" "$rt_prio" "$sock_buf"
        done
      done
    done
  done
done

awk -F'\t' '
  NR==1 {next}
  $6>=0 {
    key=$1"\t"$2"\t"$3"\t"$4"\t"$5
    n[key]++
    sum[key]+=$6
    sumsq[key]+=$6*$6
    vals[key, n[key]]=$6
  }
  END {
    print "count\tdetect_interval\tbusy_poll_us\trt_priority\tsock_buf\tn\tmean\tstd\tmedian\tmin\tmax"
    for (key in n) {
      m=n[key]
      mean=sum[key]/m
      std=(m>1)?sqrt((sumsq[key]/m)-(mean*mean)):0
      minv=vals[key,1]
      maxv=vals[key,1]
      for (i=1;i<=m;i++) {
        if (vals[key,i] < minv) minv=vals[key,i]
        if (vals[key,i] > maxv) maxv=vals[key,i]
        arr[i]=vals[key,i]
      }
      asort(arr)
      if (m%2==1) {
        median=arr[(m+1)/2]
      } else {
        median=(arr[m/2]+arr[m/2+1])/2
      }
      print key"\t"m"\t"mean"\t"std"\t"median"\t"minv"\t"maxv
      for (i=1;i<=m;i++) delete arr[i]
    }
  }
' "$OUTFILE_RAW" > "$OUTFILE_SUM"

echo "Raw results saved to $OUTFILE_RAW"
echo "Summary saved to $OUTFILE_SUM"

echo "best_by_count"
awk -F'\t' 'NR==1{next} {k=$1; v=$7; if(!(k in best)||v<best[k]){best[k]=v; line[k]=$0}} END{for(k in line) print line[k]}' "$OUTFILE_SUM" | sort -n

echo "best_by_detect_interval"
awk -F'\t' 'NR==1{next} {k=$2; v=$7; if(!(k in best)||v<best[k]){best[k]=v; line[k]=$0}} END{for(k in line) print line[k]}' "$OUTFILE_SUM" | sort -n

echo "best_by_busy_poll"
awk -F'\t' 'NR==1{next} {k=$3; v=$7; if(!(k in best)||v<best[k]){best[k]=v; line[k]=$0}} END{for(k in line) print line[k]}' "$OUTFILE_SUM" | sort -n

echo "best_by_rt_priority"
awk -F'\t' 'NR==1{next} {k=$4; v=$7; if(!(k in best)||v<best[k]){best[k]=v; line[k]=$0}} END{for(k in line) print line[k]}' "$OUTFILE_SUM" | sort -n

echo "best_by_sock_buf"
awk -F'\t' 'NR==1{next} {k=$5; v=$7; if(!(k in best)||v<best[k]){best[k]=v; line[k]=$0}} END{for(k in line) print line[k]}' "$OUTFILE_SUM" | sort -n

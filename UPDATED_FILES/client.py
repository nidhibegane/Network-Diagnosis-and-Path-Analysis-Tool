#!/usr/bin/env python3

import argparse
import json
import socket
import time

RESULT_FILE = "/tmp/diag_result.json"


def main():
    parser = argparse.ArgumentParser(description="UDP network diagnostic client")
    parser.add_argument("server_ip")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--max-rtt", type=float, default=20.0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print("NETWORK DIAGNOSTIC\n")
    print(f"Destination : {args.server_ip}:{args.port}")
    print(f"Probes      : {args.count}\n")
    print("RTTs:\n")

    rtts = []

    for seq in range(1, args.count + 1):
        start = time.perf_counter()
        message = f"PROBE,{seq},{start}"
        sock.sendto(message.encode(), (args.server_ip, args.port))

        received = False
        while True:
            remaining = args.timeout - (time.perf_counter() - start)
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(1024)
            except socket.timeout:
                break

            parts = data.decode(errors="replace").split(",")
            if len(parts) >= 3 and parts[0] == "RESPONSE" and parts[1] == str(seq):
                rtt_ms = (time.perf_counter() - start) * 1000
                rtts.append(rtt_ms)
                print(f"Probe {seq}: {rtt_ms:.3f} ms")
                received = True
                break

        if not received:
            print(f"Probe {seq}: TIMEOUT")

        time.sleep(args.interval)

    sent = args.count
    got = len(rtts)
    lost = sent - got
    loss_pct = (lost / sent) * 100 if sent else 0.0

    print("\nResults:\n")
    print(f"Packets sent     : {sent}")
    print(f"Packets received : {got}")
    print(f"Packets lost     : {lost}")
    print(f"Packet loss      : {loss_pct:.1f}%")

    if rtts:
        print(f"Minimum RTT      : {min(rtts):.3f} ms")
        print(f"Average RTT      : {sum(rtts) / len(rtts):.3f} ms")
        print(f"Maximum RTT      : {max(rtts):.3f} ms")

    avg_rtt = sum(rtts) / len(rtts) if rtts else None

    if loss_pct == 0 and avg_rtt is not None and avg_rtt > args.max_rtt:
        status = "PATH DEGRADED (HIGH DELAY)"
    elif loss_pct == 0:
        status = "PATH OPERATIONAL"
    elif loss_pct == 100:
        status = "PATH FAILED"
    else:
        status = "PATH DEGRADED"

    print(f"STATUS: {status}")

    # Save a summary so the SDN controller can correlate it with the path
    result = {
        "time": time.time(),
        "destination": f"{args.server_ip}:{args.port}",
        "sent": sent,
        "received": got,
        "lost": lost,
        "loss_pct": round(loss_pct, 1),
        "avg_rtt_ms": round(sum(rtts) / len(rtts), 3) if rtts else None,
        "status": status,
    }
    try:
        with open(RESULT_FILE, "w") as f:
            json.dump(result, f)
    except OSError as err:
        print(f"Could not save result file: {err}")


if __name__ == "__main__":
    main()

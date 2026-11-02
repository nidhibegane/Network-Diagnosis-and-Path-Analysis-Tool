#!/usr/bin/env python3
"""Send numbered UDP probes and write RTT, loss, and jitter results as JSON."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_SERVER_IP = "127.0.0.1"
DEFAULT_PORT = 5005
DEFAULT_COUNT = 10
DEFAULT_INTERVAL = 0.5
DEFAULT_TIMEOUT = 1.0
DEFAULT_OUTPUT = "udp_results.json"
RECEIVE_BUFFER_SIZE = 65535


def _validate_settings(
    port: int, count: int, interval: float, timeout: float
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if count < 1:
        raise ValueError("probe count must be at least 1")
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("probe interval must be a finite, non-negative number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than 0")


def _timestamp_string(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _valid_echo(data: bytes, sequence: int, sent_timestamp: float) -> bool:
    try:
        response = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False

    if not isinstance(response, dict):
        return False

    response_sequence = response.get("seq")
    response_timestamp = response.get("timestamp")
    return (
        type(response_sequence) is int
        and response_sequence == sequence
        and type(response_timestamp) in (int, float)
        and _is_finite_number(response_timestamp)
        and response_timestamp == sent_timestamp
    )


def _is_finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _calculate_rtt_ms(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000


def calculate_jitter(rtt_samples: Sequence[float]) -> Optional[float]:
    """Return mean absolute RTT change between consecutive successful probes."""
    if len(rtt_samples) < 2:
        return None
    changes = [
        abs(current - previous)
        for previous, current in zip(rtt_samples, rtt_samples[1:])
    ]
    return statistics.fmean(changes)


def _make_summary(probes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rtts = [
        probe["rtt_ms"] for probe in probes if probe["rtt_ms"] is not None
    ]
    sent = len(probes)
    successful = len(rtts)
    lost = sent - successful
    return {
        "total_probes_sent": sent,
        "successful_responses": successful,
        "lost_packets": lost,
        "packet_loss_percentage": (lost / sent) * 100 if sent else 0.0,
        "average_rtt_ms": statistics.fmean(rtts) if rtts else None,
        "minimum_rtt_ms": min(rtts) if rtts else None,
        "maximum_rtt_ms": max(rtts) if rtts else None,
        "jitter_ms": calculate_jitter(rtts),
        "invalid_responses": sum(
            probe["invalid_response_count"] for probe in probes
        ),
    }


def run_diagnostics(
    server_ip: str = DEFAULT_SERVER_IP,
    port: int = DEFAULT_PORT,
    count: int = DEFAULT_COUNT,
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    output: str = DEFAULT_OUTPUT,
) -> Dict[str, Any]:
    """Run sequential UDP probes, save a JSON report, and return that report."""
    _validate_settings(port, count, interval, timeout)
    addresses = socket.getaddrinfo(
        server_ip, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
    )
    if not addresses:
        raise OSError("the server name did not resolve to an IPv4 address")

    family, socket_type, protocol, _, server_address = addresses[0]
    probes: List[Dict[str, Any]] = []

    print(
        "[*] Sending {} UDP probes to {}:{} (interval={}s, timeout={}s)".format(
            count, server_ip, port, interval, timeout
        )
    )

    with socket.socket(family, socket_type, protocol) as client_socket:
        for sequence in range(1, count + 1):
            sent_epoch = time.time()
            sent_timestamp = _timestamp_string(sent_epoch)
            payload = json.dumps(
                {"seq": sequence, "timestamp": sent_epoch},
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            start_ns = time.perf_counter_ns()
            client_socket.sendto(payload, server_address)
            deadline = time.monotonic() + timeout
            invalid_response_count = 0
            rtt_ms: Optional[float] = None
            transport_error: Optional[str] = None

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                client_socket.settimeout(remaining)
                try:
                    response_data, source = client_socket.recvfrom(
                        RECEIVE_BUFFER_SIZE
                    )
                except socket.timeout:
                    break
                except ConnectionResetError:
                    transport_error = "connection_reset"
                    break

                if source[0] != server_address[0] or source[1] != server_address[1]:
                    invalid_response_count += 1
                    continue
                if not _valid_echo(response_data, sequence, sent_epoch):
                    invalid_response_count += 1
                    continue

                rtt_ms = _calculate_rtt_ms(start_ns, time.perf_counter_ns())
                break

            received = rtt_ms is not None
            probe: Dict[str, Any] = {
                "sequence_number": sequence,
                "timestamp": sent_timestamp,
                "timestamp_epoch": sent_epoch,
                "status": "received" if received else "timeout",
                "rtt_ms": rtt_ms,
                "packet_lost": not received,
                "invalid_response_count": invalid_response_count,
            }
            if transport_error is not None:
                probe["transport_error"] = transport_error
            probes.append(probe)

            if received:
                print("Probe {:>3}: received, RTT {:.3f} ms".format(sequence, rtt_ms))
            else:
                print("Probe {:>3}: timeout (packet lost)".format(sequence))

            if sequence < count and interval:
                time.sleep(interval)

    report: Dict[str, Any] = {
        "server": {"ip": server_ip, "port": port},
        "configuration": {
            "probe_count": count,
            "probe_interval_seconds": interval,
            "timeout_seconds": timeout,
        },
        "probes": probes,
        "summary": _make_summary(probes),
    }

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as report_file:
        json.dump(report, report_file, indent=2, allow_nan=False)
        report_file.write("\n")

    summary = report["summary"]
    print(
        "\n--- UDP DIAGNOSTICS ---\n"
        "Sent: {total_probes_sent} | Received: {successful_responses} | "
        "Lost: {lost_packets} ({packet_loss_percentage:.2f}%)".format(**summary)
    )
    print(
        "RTT ms (avg/min/max): {}/{}/{} | Jitter: {}".format(
            _format_metric(summary["average_rtt_ms"]),
            _format_metric(summary["minimum_rtt_ms"]),
            _format_metric(summary["maximum_rtt_ms"]),
            _format_metric(summary["jitter_ms"]),
        )
    )
    print("JSON report: {}".format(output_path))
    return report


def _format_metric(value: Optional[float]) -> str:
    return "n/a" if value is None else "{:.3f}".format(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure UDP round-trip time, packet loss, and jitter."
    )
    parser.add_argument("--server-ip", default=DEFAULT_SERVER_IP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        run_diagnostics(
            server_ip=args.server_ip,
            port=args.port,
            count=args.count,
            interval=args.interval,
            timeout=args.timeout,
            output=args.output,
        )
    except (OSError, ValueError) as error:
        print("[!] UDP diagnostics failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

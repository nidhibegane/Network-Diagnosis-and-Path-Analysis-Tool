#!/usr/bin/env python3
"""Echo valid JSON UDP probes for the network diagnostics client."""

import argparse
import json
import math
import socket
import threading
from typing import Optional


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5005
RECEIVE_BUFFER_SIZE = 65535
SOCKET_POLL_INTERVAL = 0.2


def _is_valid_probe(data: bytes) -> bool:
    try:
        probe = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False

    if not isinstance(probe, dict):
        return False

    sequence = probe.get("seq")
    timestamp = probe.get("timestamp")
    return (
        type(sequence) is int
        and sequence >= 1
        and type(timestamp) in (int, float)
        and _is_finite_number(timestamp)
        and timestamp >= 0
    )


def _is_finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def create_udp_server(host: str, port: int) -> socket.socket:
    """Bind and return a UDP socket; port 0 is supported for local tests."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        server_socket.settimeout(SOCKET_POLL_INTERVAL)
        server_socket.bind((host, port))
    except OSError:
        server_socket.close()
        raise
    return server_socket


def serve_udp(
    server_socket: socket.socket,
    stop_event: Optional[threading.Event] = None,
) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            data, address = server_socket.recvfrom(RECEIVE_BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError:
            if stop_event is not None and stop_event.is_set():
                return
            raise

        if not _is_valid_probe(data):
            print("[!] Ignoring malformed UDP probe from {}".format(address))
            continue

        server_socket.sendto(data, address)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    with create_udp_server(host, port) as server_socket:
        bound_host, bound_port = server_socket.getsockname()
        print(
            "[*] UDP echo server listening on {}:{}".format(
                bound_host, bound_port
            )
        )
        serve_udp(server_socket)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the UDP diagnostics echo server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        run_server(args.host, args.port)
    except OSError as error:
        print("[!] UDP server failed: {}".format(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

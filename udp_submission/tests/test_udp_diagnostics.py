import json
from contextlib import contextmanager
from pathlib import Path
import socket
import threading
import time
import unittest
from typing import Callable, Iterator, Optional

import udp_client
import udp_server


SUBMISSION_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = SUBMISSION_DIR / "results"


@contextmanager
def start_udp_test_server(
    handler: Optional[Callable[[bytes, tuple, socket.socket], None]] = None,
) -> Iterator[int]:
    listener = udp_server.create_udp_server("127.0.0.1", 0)
    port = listener.getsockname()[1]
    stop_event = threading.Event()
    failures = []

    def run() -> None:
        try:
            if handler is None:
                udp_server.serve_udp(listener, stop_event)
                return

            while not stop_event.is_set():
                try:
                    data, address = listener.recvfrom(65535)
                except socket.timeout:
                    continue
                handler(data, address, listener)
        except OSError as error:
            if not stop_event.is_set():
                failures.append(error)
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop_event.set()
        listener.close()
        thread.join(timeout=1)
        if thread.is_alive():
            raise AssertionError("UDP test server thread did not stop")
        if failures:
            raise AssertionError("UDP test server failed: {}".format(failures[0]))


class UdpDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    def test_normal_udp_communication(self) -> None:
        with start_udp_test_server() as port:
            report = udp_client.run_diagnostics(
                "127.0.0.1",
                port,
                count=2,
                interval=0,
                timeout=0.5,
                output=str(RESULTS_DIR / "normal_test_results.json"),
            )

        self.assertEqual(report["summary"]["successful_responses"], 2)
        self.assertEqual(report["summary"]["lost_packets"], 0)
        self.assertTrue(all(probe["status"] == "received" for probe in report["probes"]))

    def test_rtt_calculation(self) -> None:
        self.assertAlmostEqual(udp_client._calculate_rtt_ms(1_000_000, 3_500_000), 2.5)

    def test_packet_loss_is_reported_in_json(self) -> None:
        def drop_second_probe(data: bytes, address: tuple, server_socket: socket.socket) -> None:
            probe = json.loads(data.decode("utf-8"))
            if probe["seq"] != 2:
                server_socket.sendto(data, address)

        output = RESULTS_DIR / "packet_loss_test_results.json"
        with start_udp_test_server(drop_second_probe) as port:
            report = udp_client.run_diagnostics(
                "127.0.0.1",
                port,
                count=3,
                interval=0,
                timeout=0.06,
                output=str(output),
            )

        self.assertEqual(report["summary"]["lost_packets"], 1)
        self.assertAlmostEqual(report["summary"]["packet_loss_percentage"], 100 / 3)
        self.assertEqual(report["probes"][1]["status"], "timeout")
        self.assertTrue(report["probes"][1]["packet_lost"])
        saved_report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved_report, report)

    def test_jitter_uses_mean_absolute_successive_rtt_difference(self) -> None:
        self.assertEqual(udp_client.calculate_jitter([10.0, 14.0, 13.0]), 2.5)
        self.assertIsNone(udp_client.calculate_jitter([10.0]))

    def test_configurable_count_interval_and_json_output(self) -> None:
        output = RESULTS_DIR / "configured_probes_test_results.json"
        with start_udp_test_server() as port:
            report = udp_client.run_diagnostics(
                "127.0.0.1",
                port,
                count=3,
                interval=0.04,
                timeout=0.5,
                output=str(output),
            )

        self.assertEqual(len(report["probes"]), 3)
        self.assertEqual(report["configuration"]["probe_count"], 3)
        send_times = [probe["timestamp_epoch"] for probe in report["probes"]]
        self.assertGreaterEqual(send_times[1] - send_times[0], 0.035)
        self.assertGreaterEqual(send_times[2] - send_times[1], 0.035)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)

    def test_invalid_response_is_ignored_until_valid_echo(self) -> None:
        def send_invalid_then_valid(
            data: bytes, address: tuple, server_socket: socket.socket
        ) -> None:
            server_socket.sendto(b"not-json", address)
            oversized_timestamp = json.dumps(
                {"seq": 1, "timestamp": 10**400}
            ).encode("utf-8")
            server_socket.sendto(oversized_timestamp, address)
            server_socket.sendto(
                b'{"seq":1,"timestamp":' + b"9" * 5000 + b"}", address
            )
            server_socket.sendto(data, address)

        with start_udp_test_server(send_invalid_then_valid) as port:
            report = udp_client.run_diagnostics(
                "127.0.0.1",
                port,
                count=1,
                interval=0,
                timeout=0.5,
                output=str(RESULTS_DIR / "invalid_response_test_results.json"),
            )

        self.assertEqual(report["probes"][0]["status"], "received")
        self.assertEqual(report["probes"][0]["invalid_response_count"], 3)

    def test_server_ignores_malformed_packet_and_remains_available(self) -> None:
        with start_udp_test_server() as port:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(0.1)
                client.sendto(b"{malformed", ("127.0.0.1", port))
                with self.assertRaises(socket.timeout):
                    client.recvfrom(1024)

                oversized_timestamp = json.dumps(
                    {"seq": 1, "timestamp": 10**400}
                ).encode("utf-8")
                client.sendto(oversized_timestamp, ("127.0.0.1", port))
                with self.assertRaises(socket.timeout):
                    client.recvfrom(1024)
                client.sendto(
                    b'{"seq":1,"timestamp":' + b"9" * 5000 + b"}",
                    ("127.0.0.1", port),
                )
                with self.assertRaises(socket.timeout):
                    client.recvfrom(1024)

                valid_probe = json.dumps(
                    {"seq": 1, "timestamp": time.time()}
                ).encode("utf-8")
                client.settimeout(0.5)
                client.sendto(valid_probe, ("127.0.0.1", port))
                response, _ = client.recvfrom(1024)
                self.assertEqual(response, valid_probe)

    def test_unavailable_echo_service_is_reported_as_timeout(self) -> None:
        unused_socket = udp_server.create_udp_server("127.0.0.1", 0)
        port = unused_socket.getsockname()[1]
        unused_socket.close()
        report = udp_client.run_diagnostics(
            "127.0.0.1",
            port,
            count=2,
            interval=0,
            timeout=0.04,
            output=str(RESULTS_DIR / "unavailable_test_results.json"),
        )

        self.assertEqual(report["summary"]["successful_responses"], 0)
        self.assertEqual(report["summary"]["lost_packets"], 2)
        self.assertEqual(report["summary"]["packet_loss_percentage"], 100.0)
        self.assertIsNone(report["summary"]["average_rtt_ms"])
        self.assertTrue(all(probe["status"] == "timeout" for probe in report["probes"]))


if __name__ == "__main__":
    unittest.main()

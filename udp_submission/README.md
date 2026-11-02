# UDP diagnostics component

This folder contains the Person 1 UDP client/server implementation. It uses
only the Python standard library and does not change or call the teammate's
REST API or SDN controller.

## Requirements

- Python 3.9 or newer
- UDP port 5005 available (or select another port with `--port`)

No third-party packages are required.

## Run locally

Open two terminals in this folder.

Terminal 1:

```powershell
python udp_server.py --host 127.0.0.1 --port 5005
```

Terminal 2:

```powershell
python udp_client.py --server-ip 127.0.0.1 --port 5005 --count 10 --interval 0.5 --timeout 1 --output results\local_run.json
```

For the captured three-probe run, use:

```powershell
python udp_client.py --server-ip 127.0.0.1 --port 5005 --count 3 --interval 0.3 --timeout 1 --output results\submission_localhost_results.json
```

The server defaults to `0.0.0.0:5005`; the client defaults to
`127.0.0.1:5005`, 10 probes, a 0.5-second interval, a 1-second timeout, and
`udp_results.json`. Use `python udp_client.py --help` and
`python udp_server.py --help` for all options. On separate physical systems,
start the server with `--host 0.0.0.0`, pass its reachable IPv4 address to
`--server-ip`, and allow inbound UDP on the chosen port in the server firewall.

Each probe packet is UTF-8 JSON with a sequence number and Unix timestamp. The
server validates both fields and echoes valid packets unchanged. The client
accepts only matching sequence/timestamp echoes from the configured server;
malformed or mismatched datagrams are ignored until the probe deadline.

## Measurements and JSON

Each probe in the report has a sequence number, UTC timestamp, epoch timestamp,
`received` or `timeout` status, RTT in milliseconds (or JSON `null`), a
`packet_lost` boolean, and the count of invalid responses seen for that probe.
The summary contains sent/success/lost counts, loss percentage, average/minimum/
maximum RTT, jitter, and invalid-response count. RTT summary fields and jitter
are `null` when there are not enough successful samples.

Jitter is the arithmetic mean of the absolute differences between consecutive
successful RTT samples:

`jitter = mean(abs(RTT[i] - RTT[i-1]))`

Successful samples are considered in probe order; timed-out probes do not add
an RTT sample. The report is written to the path provided with `--output`.

## Automated tests

From this folder, run:

```powershell
python -m unittest discover -s tests -v
```

The tests use only localhost. They exercise the echo server and client, RTT
and jitter calculations, intentional packet dropping, configurable count and
interval, JSON serialization, malformed datagrams, and an unresponsive UDP
service. Actual integration-test JSON reports are written under `results\`.

## Mininet link-failure and recovery procedure

Mininet is not installed in the environment where these deliverables were
prepared, so no Mininet link-failure/recovery result is claimed here. Run this
procedure in the teammate's Linux/Mininet environment:
The Mininet link-failure and recovery test remains pending access to the
teammate's Mininet topology and controller configuration.

1. Copy this folder to a path available to the Mininet host processes.
   Substitute that absolute Linux path for `/path/to/udp_submission` below.
   Confirm the actual host names and choose the active-path switch link from
   the teammate's topology; `s1 s2` below is only an example.
2. Start the server on System 3 (shown as `h3`) and leave it running:

   ```text
   mininet> h3 python3 /path/to/udp_submission/udp_server.py --host 0.0.0.0 --port 5005 > /tmp/udp-server.log 2>&1 &
   ```

3. In a separate Mininet terminal, start a long client run on System 1
   (shown as `h1`). Replace `10.0.0.3` with System 3's reachable host IP and
   use an output path writable by that host:

   ```text
   mininet> h1 python3 /path/to/udp_submission/udp_client.py --server-ip 10.0.0.3 --port 5005 --count 120 --interval 0.5 --timeout 1 --output /tmp/udp-mininet-results.json > /tmp/udp-client.log 2>&1 &
   ```

4. Allow an initial healthy period, disable the selected active-path link, and
   wait long enough for missed probes. Restore the link after the teammate's
   controller has had time to install an alternate route (if configured):

   ```text
   mininet> sh sleep 8
   mininet> link s1 s2 down
   mininet> sh sleep 8
   mininet> link s1 s2 up
   ```

   The exact link and whether it is restored manually or by the controller
   must match the teammate's topology/controller setup. Do not disable an
   unrelated link. Leave the client running long enough to observe responses
   resume, then inspect `/tmp/udp-mininet-results.json` and
   `/tmp/udp-client.log` on the Mininet host.

Expected observations: the normal period has received probes; the failed path
causes timeouts and increased loss if there is no immediate alternate path;
after link restoration or successful SDN reroute, received probes resume.
Recovery behavior depends on the teammate's controller and topology and is
not implemented by this UDP component. Adapt node names, IPs, paths, and
switch-link names to the actual Mininet environment. Keep the UDP server
running throughout.

## Evidence

`results\` contains JSON generated by actually executed localhost tests, and
`results\test_execution.txt` records the test runner output. Actual terminal
screenshots from the localhost server/client run, JSON report, and automated
test run are in `screenshots\`:

- `01_udp_server_running.png` — server listening on localhost UDP port 5005.
- `02_udp_client_3_probes.png` — three successful probes, RTTs, loss, jitter,
  and final summary.
- `03_udp_json_report.png` — JSON probe entries and summary loaded from the
  generated report.
- `04_udp_tests_8_passed.png` — test output reporting 8 passed and 0 failed.

No Mininet or link-failure result has been fabricated.

## Diagnostic tool (`src/diagnostic_tool.py`)

Reads the Ryu controller's REST API and the UDP client's results, and prints one report: topology, candidate paths, measurements, failed links and paths, the active path (from port counters), and a comparison with the previous run. It is read-only and never changes flows.

### Requirements

- The controller is running with the REST apps (see "Run" below) and Mininet is up.
- `python3` on the machine that runs the tool. No virtual environment is needed.
- `udp_server.py` is running on h2 (`sdn_topo.py` starts it) and `udp_client.py` has written `/tmp/diag_result.json`.

### Run order

Use three terminals, all in `src/`.

1. **Controller** (terminal 1, Ryu virtual environment active):
   ```bash
   cd ~/sdn_project/src
   source ~/ryu39/bin/activate
   ryu-manager --observe-links --ofp-tcp-listen-port 6653 --wsapi-port 8080 \
       ryu.app.ofctl_rest ryu.app.rest_topology reroute_controller.py
   ```
2. **Mininet** (terminal 2, no virtual environment):
   ```bash
   cd ~/sdn_project/src
   sudo python3 sdn_topo.py --topo multi
   ```
   Wait about 10 seconds, then run `pingall` (expect 0% dropped).
3. **Baseline, normal run** (terminal 3). Use `--reset-baseline` on the healthy run only:
   ```bash
   cd ~/sdn_project/src
   python3 diagnostic_tool.py --ryu 127.0.0.1:8080 --from-file /tmp/diag_result.json \
       --src-dpid 1 --dst-dpid 5 --baseline-file base_multi.json --reset-baseline --label "normal"
   ```
   When it prints "Run the probes in h1 NOW", run this at the `mininet>` prompt, then press Enter in terminal 3:
   ```
   h1 python3 udp_client.py 10.0.0.2 --count 10
   ```
4. **After a failure** (for example `link s1 s2 down` in Mininet), run the tool again without `--reset-baseline`:
   ```bash
   python3 diagnostic_tool.py --ryu 127.0.0.1:8080 --from-file /tmp/diag_result.json \
       --src-dpid 1 --dst-dpid 5 --baseline-file base_multi.json --label "s1-s2 down"
   ```
   Run the UDP client again when it pauses, then press Enter.

### Options

| Option | Meaning |
|---|---|
| `--ryu HOST:PORT` | Ryu REST address (here `127.0.0.1:8080`) |
| `--from-file PATH` | UDP client result file (here `/tmp/diag_result.json`) |
| `--src-dpid N`, `--dst-dpid N` | Source and destination switch IDs for path analysis |
| `--baseline-file PATH` | Known-good topology used to detect failed links |
| `--reset-baseline` | Save the current topology as the baseline. Use on the healthy run only |
| `--label TEXT` | Name of the run, shown in the report and in the comparison |

### Topologies and switch IDs

| Topology | Start command | Source and destination | Baseline file |
|---|---|---|---|
| Multi-path (5 switches) | `sudo python3 sdn_topo.py --topo multi` | `--src-dpid 1 --dst-dpid 5` | `base_multi.json` |
| Single path (3 switches) | `sudo python3 sdn_topo.py --topo single` | `--src-dpid 1 --dst-dpid 3` | `base_single.json` |
| Multi-path with delay and loss | `sudo python3 sdn_topo.py --topo multi --delay 20ms --loss 5` | `--src-dpid 1 --dst-dpid 5` | `base_delay.json` |

### Reading the report

| Section | Content |
|---|---|
| [1] SDN topology | Switches, links and hosts the controller currently sees |
| [2] Path analysis | Candidate paths from source to destination, each UP or FAILED |
| [3] Probe results | Pause while the UDP probes run in h1 |
| [4] Measurements | Sent, received and lost packets, loss %, RTT, jitter, status |
| [5] Correlation | Active path, FAILED LINKS, FAILED PATHS, the likely cause, suggested reroute |
| [6] Comparison | Loss, RTT and path against the previous run |

Notes:

- Use a separate baseline file for each topology.
- The comparison in [6] uses `diagnostic_history.json`, which is shared by all runs, so it is only meaningful against a run from the same scenario.
- `sudo mn -c` also stops `ryu-manager` and deletes `/tmp/*.log`. Start the controller after `mn -c`, and copy `/tmp/sdn_events.log` into `results/` before running it.
- Evidence for every scenario is in `screenshots/` (numbered 01 to 20) and `results/`.

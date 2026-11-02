#!/usr/bin/env python3

import json
import os
import time

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER
from os_ken.controller.handler import MAIN_DISPATCHER
from os_ken.controller.handler import set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.topology import api as topology_api
from os_ken.topology import switches
from os_ken.lib import hub


class MultipathController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'switches': switches.Switches}

    FLOW_PRIORITY = 10
    DROP_PRIORITY = 100
    DROP_CONTROL_FILE = '/tmp/sdn_drop'
    PATH_CONTROL_FILE = '/tmp/sdn_path'
    RESULT_FILE = '/tmp/diag_result.json'

    # Fixed host layout of topology_multipath.py
    H1_MAC = '00:00:00:00:00:01'
    H2_MAC = '00:00:00:00:00:02'
    H1_SWITCH = 1
    H1_PORT = 1
    H2_SWITCH = 5
    H2_PORT = 4
    SRC = 's1'
    DST = 's5'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph = {}
        self.ports = {}
        self.datapaths = {}
        self.selected = 1
        self.active_path = None
        self.drop_hits = {}
        self.last_hits = {}
        self.last_result_time = None
        self.empty_logged = False
        self.failed_switches = set()
        self.start_time = time.time()

        # Forget control files left over from earlier runs
        for name in (self.DROP_CONTROL_FILE, self.PATH_CONTROL_FILE):
            try:
                os.remove(name)
            except OSError:
                pass

        self.monitor_thread = hub.spawn(self.topology_monitor)
        self.drop_thread = hub.spawn(self.drop_control_loop)
        self.path_thread = hub.spawn(self.path_control_loop)
        self.report_thread = hub.spawn(self.correlation_loop)

    # ---------- flow helper ----------

    def add_flow(self, datapath, priority, match, actions):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        instructions = [
            parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)
        ]
        flow = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions
        )
        datapath.send_msg(flow)

    # ---------- switch connection ----------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser

        # Unknown traffic goes to the controller, which ignores it.
        # There is no flooding, so loops cannot form.
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)
        ]
        self.add_flow(datapath, 0, match, actions)
        self.datapaths[datapath.id] = datapath

    # ---------- topology discovery ----------

    def topology_monitor(self):
        last = None
        while True:
            hub.sleep(5)

            switch_list = topology_api.get_switch(self, None)
            link_list = topology_api.get_link(self, None)

            if not link_list:
                if self.graph and not self.empty_logged:
                    self.empty_logged = True
                    self.logger.info(
                        'DISCOVERY EMPTY: keeping last known topology')
                continue
            self.empty_logged = False

            names = sorted('s%s' % sw.dp.id for sw in switch_list)
            graph = {name: [] for name in names}
            ports = {}
            link_names = []

            for link in link_list:
                a = link.src.dpid
                b = link.dst.dpid
                src = 's%s' % a
                dst = 's%s' % b
                graph.setdefault(src, [])
                if dst not in graph[src]:
                    graph[src].append(dst)
                ports[(a, b)] = link.src.port_no
                link_names.append('%s <-> %s' % (dst, src))

            self.graph = graph
            self.ports = ports

            summary = (names, sorted(link_names))
            if summary != last:
                last = summary
                self.logger.info('DISCOVERED SWITCHES: %s', names)
                self.logger.info('DISCOVERED LINKS: %s', sorted(link_names))

                paths = self.all_paths()
                if paths:
                    self.logger.info('PATHS FOUND: %s', len(paths))
                    for i, p in enumerate(paths, 1):
                        self.logger.info('  Path %s: %s', i, ' -> '.join(p))
                else:
                    self.logger.info('PATHS FOUND: none')

                self.apply_selection()

    def all_paths(self):
        paths = []

        def walk(node, path):
            if node == self.DST:
                paths.append(path)
                return
            for neighbor in sorted(self.graph.get(node, [])):
                if neighbor not in path:
                    walk(neighbor, path + [neighbor])

        if self.SRC in self.graph and self.DST in self.graph:
            walk(self.SRC, [self.SRC])

        paths.sort(key=lambda p: (len(p), p))
        return paths

    # ---------- installing a path ----------

    def clear_host_flows(self, dp):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        for mac in (self.H1_MAC, self.H2_MAC):
            flow = parser.OFPFlowMod(
                datapath=dp,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch(eth_dst=mac)
            )
            dp.send_msg(flow)

    def install_path(self, path):
        dpids = [int(name[1:]) for name in path]

        if dpids[0] != self.H1_SWITCH or dpids[-1] != self.H2_SWITCH:
            return False

        for d in dpids:
            if d not in self.datapaths:
                self.logger.info('INSTALL: switch s%s not connected yet', d)
                return False

        plan = []
        for i, d in enumerate(dpids):
            if i == len(dpids) - 1:
                forward_port = self.H2_PORT
            else:
                forward_port = self.ports.get((d, dpids[i + 1]))

            if i == 0:
                reverse_port = self.H1_PORT
            else:
                reverse_port = self.ports.get((d, dpids[i - 1]))

            if forward_port is None or reverse_port is None:
                self.logger.info('INSTALL: port unknown on s%s', d)
                return False

            plan.append((d, self.H2_MAC, forward_port))
            plan.append((d, self.H1_MAC, reverse_port))

        for dp in list(self.datapaths.values()):
            self.clear_host_flows(dp)

        for d, mac, port in plan:
            dp = self.datapaths[d]
            parser = dp.ofproto_parser
            self.add_flow(
                dp,
                self.FLOW_PRIORITY,
                parser.OFPMatch(eth_dst=mac),
                [parser.OFPActionOutput(port)]
            )
        return True

    def apply_selection(self):
        paths = self.all_paths()
        if not paths:
            return

        idx = self.selected
        if idx < 1 or idx > len(paths):
            self.logger.info(
                'PATH SELECT: path %s does not exist (%s found)',
                idx, len(paths))
            return

        path = paths[idx - 1]
        if self.install_path(path):
            self.active_path = path
            self.logger.info('ACTIVE PATH: Path %s = %s',
                             idx, ' -> '.join(path))

    def path_control_loop(self):
        # File content is a path number, for example: 2
        last = None
        while True:
            hub.sleep(1)
            if not self.is_fresh(self.PATH_CONTROL_FILE):
                continue
            try:
                with open(self.PATH_CONTROL_FILE) as f:
                    value = int(f.read().strip())
            except (OSError, ValueError):
                continue

            if value != last:
                last = value
                if value != self.selected:
                    self.selected = value
                    self.apply_selection()

    # ---------- SDN drop rule ----------

    def is_fresh(self, name):
        # A control file counts only if written after this controller started
        try:
            return os.path.getmtime(name) > self.start_time
        except OSError:
            return False

    def drop_match(self, parser):
        # UDP diagnostic traffic towards the server (port 5000)
        return parser.OFPMatch(eth_type=0x0800, ip_proto=17, udp_dst=5000)

    def install_drop_rule(self, dpid):
        dp = self.datapaths.get(dpid)
        if dp is None:
            self.logger.info('DROP: switch %s not connected', dpid)
            return False
        parser = dp.ofproto_parser
        # A flow entry with no instructions means "drop"
        flow = parser.OFPFlowMod(
            datapath=dp,
            priority=self.DROP_PRIORITY,
            match=self.drop_match(parser),
            instructions=[]
        )
        dp.send_msg(flow)
        self.logger.info('DROP RULE INSTALLED on s%s (udp_dst=5000)', dpid)
        return True

    def remove_drop_rule(self, dpid):
        dp = self.datapaths.get(dpid)
        if dp is None:
            return
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        flow = parser.OFPFlowMod(
            datapath=dp,
            command=ofp.OFPFC_DELETE_STRICT,
            out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY,
            priority=self.DROP_PRIORITY,
            match=self.drop_match(parser)
        )
        dp.send_msg(flow)
        self.logger.info('DROP RULE REMOVED from s%s', dpid)
        self.failed_switches.discard('s%s' % dpid)

    def drop_control_loop(self):
        # File content looks like: "on 2" or "off 2" (action, switch id)
        active = set()
        while True:
            hub.sleep(1)
            if not self.is_fresh(self.DROP_CONTROL_FILE):
                continue
            try:
                with open(self.DROP_CONTROL_FILE) as f:
                    action, dpid = f.read().split()
                dpid = int(dpid)
            except ValueError:
                continue

            if action == 'on' and dpid not in active:
                if self.install_drop_rule(dpid):
                    active.add(dpid)
            elif action == 'off' and dpid in active:
                self.remove_drop_rule(dpid)
                active.discard(dpid)

    # ---------- correlation of UDP result with the SDN path ----------

    def read_result(self):
        try:
            with open(self.RESULT_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def correlation_loop(self):
        # Ignore a result file left over from an earlier run
        old = self.read_result()
        if old:
            self.last_result_time = old.get('time')

        while True:
            hub.sleep(2)
            result = self.read_result()
            if not result:
                continue
            if result.get('time') == self.last_result_time:
                continue
            self.last_result_time = result.get('time')

            self.request_drop_stats()
            hub.sleep(1)
            self.print_report(result)

    def request_drop_stats(self):
        self.drop_hits = {}
        for dp in list(self.datapaths.values()):
            ofp = dp.ofproto
            parser = dp.ofproto_parser
            req = parser.OFPFlowStatsRequest(
                dp,
                flags=0,
                table_id=ofp.OFPTT_ALL,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                cookie=0,
                cookie_mask=0,
                match=parser.OFPMatch()
            )
            dp.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        for stat in ev.msg.body:
            if stat.priority == self.DROP_PRIORITY:
                self.drop_hits[dpid] = (
                    self.drop_hits.get(dpid, 0) + stat.packet_count
                )

    def maybe_reroute(self, result, culprits):
        # Automatic rerouting: if the probe failed completely and a drop
        # rule was hit, avoid those switches and use another path.
        if result.get('loss_pct', 0) < 100 or not culprits:
            return
        for d in culprits:
            self.failed_switches.add('s%s' % d)

        avoid = ', '.join(sorted(self.failed_switches))
        for i, p in enumerate(self.all_paths(), 1):
            if not (set(p) & self.failed_switches):
                self.logger.info(
                    'AUTO-REROUTE: avoiding %s, switching to Path %s',
                    avoid, i)
                self.selected = i
                self.apply_selection()
                return
        self.logger.info('AUTO-REROUTE: no path avoids %s', avoid)

    def print_report(self, result):
        path = self.active_path
        paths = self.all_paths()

        # Drop hits since the previous report
        deltas = {}
        for dpid, count in self.drop_hits.items():
            last = self.last_hits.get(dpid, 0)
            deltas[dpid] = count - last if count >= last else count
        self.last_hits = dict(self.drop_hits)

        culprits = sorted(d for d, n in deltas.items() if n > 0)

        if path:
            shown = []
            for name in path:
                if int(name[1:]) in culprits:
                    shown.append('[%s DROP]' % name)
                else:
                    shown.append(name)
            number = paths.index(path) + 1 if path in paths else '?'
            path_text = 'Path %s of %s: %s' % (
                number, len(paths), ' -> '.join(shown))
            plain = ' -> '.join(path)
        else:
            path_text = 'unknown (no active path)'
            plain = 'unknown'

        hits_text = ', '.join(
            's%s=%s' % (d, deltas[d]) for d in sorted(deltas)
        ) or 'none'

        loss = result.get('loss_pct', 0)
        names = ', '.join('s%s' % d for d in culprits)

        if loss == 0 and 'HIGH DELAY' in str(result.get('status')):
            verdict = ('HIGH DELAY: avg RTT %s ms on %s (cause is not an '
                       'SDN drop rule)' % (result.get('avg_rtt_ms'), plain))
        elif loss == 0:
            verdict = 'PATH HEALTHY: %s' % plain
        elif culprits:
            verdict = 'FAILED PATH: %s (SDN drop rule hit on %s)' % (
                plain, names)
        else:
            verdict = ('LOSS WITHOUT DROP-RULE HITS: cause is not an '
                       'SDN drop rule')

        self.logger.info('=== CORRELATION REPORT ===')
        self.logger.info(
            'UDP probe : %s sent=%s lost=%s loss=%s%% avg_rtt=%s ms',
            result.get('destination'), result.get('sent'),
            result.get('lost'), loss, result.get('avg_rtt_ms'))
        self.logger.info('Status    : %s', result.get('status'))
        self.logger.info('SDN path  : %s', path_text)
        self.logger.info('Drop hits : %s', hits_text)
        self.logger.info('Verdict   : %s', verdict)
        self.logger.info('==========================')
        self.maybe_reroute(result, culprits)

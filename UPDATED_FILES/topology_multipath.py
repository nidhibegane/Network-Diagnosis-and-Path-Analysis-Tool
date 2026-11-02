#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel


def create_network():
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        autoSetMacs=True
    )

    net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653
    )

    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')

    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', protocols='OpenFlow13')
    s5 = net.addSwitch('s5', protocols='OpenFlow13')

    # The order of these lines decides the port numbers
    net.addLink(h1, s1)   # s1 port 1
    net.addLink(s1, s2)   # s1 port 2, s2 port 1
    net.addLink(s1, s3)   # s1 port 3, s3 port 1
    net.addLink(s1, s4)   # s1 port 4, s4 port 1
    net.addLink(s2, s5)   # s2 port 2, s5 port 1
    net.addLink(s3, s5)   # s3 port 2, s5 port 2
    net.addLink(s4, s5)   # s4 port 2, s5 port 3
    net.addLink(h2, s5)   # s5 port 4

    net.start()

    # Fixed ARP entries, so no broadcast traffic loops around
    h1.setARP(h2.IP(), h2.MAC())
    h2.setARP(h1.IP(), h1.MAC())

    print('Multipath SDN topology (3 paths):')
    print('  Path 1: h1 - s1 - s2 - s5 - h2')
    print('  Path 2: h1 - s1 - s3 - s5 - h2')
    print('  Path 3: h1 - s1 - s4 - s5 - h2')

    CLI(net)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    create_network()

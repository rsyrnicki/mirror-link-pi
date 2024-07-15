#!/usr/bin/env python3

# py-lsupnp.py
# Simple command-line program to discover all UPnP devices on a network.
#
# Copyright (c) 2018, Chris Coffey <kpuc@sdf.org>
#
# Permission to use, copy, modify, and/or distribute this software
# for any purpose with or without fee is hereby granted, provided
# that the above copyright notice and this permission notice appear
# in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL 
# WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
# AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL
# DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA
# OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER
# TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
# PERFORMANCE OF THIS SOFTWARE.
#

"""A quick way to enumerate UPnP-enabled devices on a network"""

import sys
import socket
import ipaddress
import argparse

SSDP_MULTICAST_IP = b'239.255.255.250'
SSDP_MULTICAST_PORT = 1900
DEFAULT_TIMEOUT = 4.0
RECV_BUF_SIZE = 2048

SSDP_DISCOVER_STRING = (b'M-SEARCH * HTTP/1.1\r\n' +
                        b'HOST: 239.255.255.250:1900\r\n' +
                        b'MAN: "ssdp:discover"\r\n' +
                        b'MX: 3\r\n' +
                        b'ST: ssdp:all\r\n' +
                        b'\r\n')

class lsupnp:
    """Enumerate UPnP devices.
    
    'lsupnp' is short for 'list UPnP', in the same vein as lsusb, lsscsi, etc.
    """

    def __init__(self):
        """Instantiate an lsupnp object.

        All command-line options get their default values here.
        """
        self.opt_port = 0
        self.opt_rdns = False
        self.opt_verbose = False
        self.opt_timeout = DEFAULT_TIMEOUT

    def __str__(self):
        return "{0}: port={1} rdns={2} verbose={3} timeout={4}".format( \
            self.__class__.__name__, self.opt_port, self.opt_rdns, 
            self.opt_verbose, self.opt_timeout)

    def discover_hosts(self):
        """Discover all UPnP-enabled devices on a network.

        Returns:
            0 on success, errno on exception.
        """
        socket.setdefaulttimeout(self.opt_timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.opt_port > 0:
            try:
                sock.bind(('', self.opt_port))
            except OSError as e:
                print("Socket bind failed: {0}".format(e))
                return e.errno

        sock.sendto(SSDP_DISCOVER_STRING, (SSDP_MULTICAST_IP, SSDP_MULTICAST_PORT))

        try:
            hosts = []

            while True:
                data, server = sock.recvfrom(RECV_BUF_SIZE)

                # Get the IP address
                if server[0] not in hosts:
                    hosts.append(server[0])

                if self.opt_verbose == True:
                    print('{0}:{1}'.format(server[0], server[1]))
                    print('{0}'.format(data.decode(sys.stdout.encoding)))

        except socket.timeout:
            if self.opt_verbose == True:
                print("Socket timed out after {0:.1f} seconds".format(self.opt_timeout))
        finally:
            sock.close()

            # Do a natural sort on the IP addresses
            hosts = sorted(ipaddress.ip_address(host) for host in hosts)
            for host in hosts:
                print("{0}\t".format(host), end='')
                if self.opt_rdns == True:
                    print("{0}".format(self._rdns_lookup(str(host))), end='')
                print("")

        return 0

    def _rdns_lookup(self, ip):
        """Do a reverse DNS lookup (IP address to hostname) on the specified IP address.

        Args:
            ip: string of an IP address.

        Returns:
            A string of the associated hostname. May also be blank if no hostname found, 
            or exception data if verbose output is enabled.
        """
        hostname = ""
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except socket.herror as e:
            if self.opt_verbose == True:
                hostname = e
        return hostname


def main():
    """Main routine"""
    parser = argparse.ArgumentParser(description='Discover and list UPnP devices on the network.')
    parser.add_argument('-p', '--port', dest='opt_port', action='store', type=int, metavar='PORT',
                        help='Specify client-side UDP port to bind to. Useful for getting through firewalls.')
    parser.add_argument('-r', '--rdns', dest='opt_rdns', action='store_true',
                        help='Do reverse DNS lookups on discovered hosts')
    parser.add_argument('-t', '--timeout', dest='opt_timeout', action='store', type=float, metavar='TIMEOUT',
                        help='Specify socket timeout interval in seconds (default is {0})'.format(DEFAULT_TIMEOUT))
    parser.add_argument('-v', '--verbose', dest='opt_verbose', action='store_true',
                        help='Display verbose information')

    l = lsupnp()
    parser.parse_args(namespace=l)

    if l.opt_verbose == True:
        print(l)

    return l.discover_hosts()


if __name__ == '__main__':
    sys.exit(main())

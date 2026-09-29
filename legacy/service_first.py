import socket
import logging

# LOGGING
logging.basicConfig(filename='/srv/mlpi/mlpi.log', encoding='utf-8', level=logging.DEBUG)
logging.debug('Initiating log file: mlpi.log (DEBUG)')
msg = (
    'M-SEARCH * HTTP/1.1\r\n'
    'HOST:239.255.255.250:1900\r\n'
    'ST:upnp:rootdevice\r\n'
    'MX:2\r\n'
    'MAN:"ssdp:discover"\r\n'
    '\r\n'
)

# Set up UDP socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
s.settimeout(2)

# In Python 3, we need to encode the message to bytes
s.sendto(msg.encode('utf-8'), ('239.255.255.250', 1900))

# Bind the socket to the IP address of the specific network interface
interface_ip = '192.168.7.2'  # Replace with your interface IP
#s.bind((interface_ip, 0))
s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))


while True:
    try:
        data, addr = s.recvfrom(65507)
        # In Python 3, data received from sockets is in bytes, decode it to string
        print(addr, data.decode('utf-8'))
        logging.debug(addr, data.decode('utf-8'))
    except socket.timeout:
        logging.debug("timeout")
        pass

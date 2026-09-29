import socket
import netifaces as ni
import logging

# LOGGING
logging.basicConfig(filename='/srv/mlpi/mlpi.log', encoding='utf-8', level=logging.DEBUG)
logging.debug('Initiating log file: mlpi.log (DEBUG)')

def get_interface_ip(interface_name):
    return ni.ifaddresses(interface_name)[ni.AF_INET][0]['addr']

def main():
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

    # Specify the network interface
    interface_name = 'usb0'  # Replace with your interface name
    interface_ip = get_interface_ip(interface_name)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))

    # Send the message
    s.sendto(msg.encode('utf-8'), ('239.255.255.250', 1900))

    time = "now"
    with open('/srv/mlpi/upnp_responses.txt', 'w') as f:
        print(f" Starting mlpi {time}")
        while True:
            try:
                logging.debug("Waiting for a message")
                data, addr = s.recvfrom(65507)
                response = data.decode('utf-8')
                print(addr, response)
                f.write(f"From {addr}:\n{response}\n\n")
                logging.debug(f"From {addr}:\n{response}\n\n")
            except socket.timeout:
                logging.debug("Timeout: No more responses.")

if __name__ == "__main__":
    main()

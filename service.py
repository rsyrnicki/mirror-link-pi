import socket
import netifaces as ni
import logging

# LOGGING
logging.basicConfig(filename='/srv/mlpi/mlpi.log', encoding='utf-8', level=logging.DEBUG)
logging.debug('Initiating log file: mlpi.log (DEBUG)')

def get_interface_ip(interface_name):
    return ni.ifaddresses(interface_name)[ni.AF_INET][0]['addr']

# SSDP:alive message
def send_ssdp_alive():
    message = '\r\n'.join([
        'NOTIFY * HTTP/1.1',
        'HOST: 239.255.255.250:1900',
        'NT: upnp:rootdevice',
        'NTS: ssdp:alive',
        'USN: uuid:c8cba096-5abe-47ac-9c14-3267d7c94ce6::upnp:rootdevice',
        'LOCATION: http://192.168.7.2/',
        'CACHE-CONTROL: max-age=1800',
        'SERVER: Python/3.x UPnP/1.0',
        '',
        ''
    ]).encode('utf-8')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.sendto(message, ('239.255.255.250', 1900))

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
    s.settimeout(10)

    # Specify the network interface
    interface_name = 'usb0'  # Replace with your interface name
    interface_ip = get_interface_ip(interface_name)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 1900))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, 
                 socket.inet_aton('239.255.255.250') + socket.inet_aton(interface_ip))

    # Send the message
    #print(f"Sending M-SEARCH from {interface_ip}")
    logging.debug(f"Sending M-SEARCH from {interface_ip}")
    s.sendto(msg.encode('utf-8'), ('239.255.255.250', 1900))

    while True:
        try:
            while True:
                try:
                    data, addr = s.recvfrom(65507)
                    response = data.decode('utf-8')
                    #print(f"Received response from {addr}")
                    logging.debug(f"Received response from {addr}")
                    logging.debug(f"From {addr}:\n{response}\n\n")
                except socket.timeout:
                    logging.debug("No more responses, exiting.")
                    #print("No more responses, exiting.")
                    break
                try: 
                    send_ssdp_alive()
                except Exception as e:
                    loging.debug(f"Exception {e} while trying to send_ssdp_alive()")
        except Exception as e:
            #print(f"An error occurred: {e}")
            logging.debug(f"An error occurred: {e}")
        time.sleep(5)

if __name__ == "__main__":
    main()

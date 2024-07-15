import socket

# Simulated UPnP response message
response = (
    'HTTP/1.1 200 OK\r\n'
    'CACHE-CONTROL: max-age=1800\r\n'
    'DATE: Sat, 30 May 2020 15:38:29 GMT\r\n'
    'EXT:\r\n'
    'LOCATION: http://192.168.1.2:12345/device.xml\r\n'
    'SERVER: Custom/1.0 UPnP/1.0 Proc/Ver\r\n'
    'ST: upnp:rootdevice\r\n'
    'USN: uuid:unique-service-name::upnp:rootdevice\r\n'
    '\r\n'
)

def main():
    # Set up UDP socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    # IP address and port of the Raspberry Pi
    pi_ip = '192.168.7.2'  # Replace with your Raspberry Pi's IP address
    pi_port = 1900

    # Send the simulated UPnP response message
    print(f"Sending simulated UPnP response to {pi_ip}:{pi_port}")
    s.sendto(response.encode('utf-8'), (pi_ip, pi_port))

if __name__ == "__main__":
    main()

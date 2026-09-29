import socket, sys, time

host, port, probe, outfile = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]

s = socket.socket()
s.settimeout(5)
s.connect((host, port))

# Drain the server's initial hello (up to 1 s).
hello = b""
s.settimeout(1.0)
try:
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        hello += chunk
except socket.timeout:
    pass

# Convert literal "\n" tokens in the probe argument into real newlines so we
# can pass envelopes from a shell script via argv without escaping nightmares.
probe_bytes = probe.encode().replace(b"\\n", b"\n")
s.sendall(probe_bytes)
time.sleep(0.1)

response = b""
s.settimeout(5.0)
try:
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        response += chunk
except socket.timeout:
    pass

try:
    s.shutdown(socket.SHUT_WR)
except OSError:
    pass
s.close()

with open(outfile, "wb") as f:
    f.write(f"### HELLO ({len(hello)} bytes) ###\n".encode())
    f.write(hello)
    if not hello.endswith(b"\n"):
        f.write(b"\n")
    f.write(f"### PROBE SENT ({len(probe_bytes)} bytes) ###\n".encode())
    f.write(probe_bytes)
    if not probe_bytes.endswith(b"\n"):
        f.write(b"\n")
    f.write(f"### RESPONSE ({len(response)} bytes) ###\n".encode())
    f.write(response)
    if not response.endswith(b"\n"):
        f.write(b"\n")
    f.write(b"### END ###\n")

print(f"hello={len(hello)} probe={len(probe_bytes)} response={len(response)}",
      file=sys.stderr)

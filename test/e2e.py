#!/usr/bin/env python3
import os
import pty
import socket
import subprocess
import sys
import time

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "ghcr.io/jenserat/ser2net:ci-test"
CONTAINER_NAME = "ser2net-e2e"
HOST_PORT = 13001
SERIAL_DEV = "/dev/ttyS0"
CONFIG_PATH = os.path.abspath("test/ser2net.yaml")
TIMEOUT = 30


def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def cleanup():
    run(["docker", "rm", "-f", CONTAINER_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fail(msg):
    print(f"ERROR: {msg}", flush=True)
    run(["docker", "logs", CONTAINER_NAME], stdout=sys.stdout, stderr=sys.stderr)
    cleanup()
    sys.exit(1)


def main():
    cleanup()

    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.chmod(slave_name, 0o666)
    print(f"Using PTY slave: {slave_name}", flush=True)

    # Try to pull only if it doesn't exist locally
    inspect = run(["docker", "image", "inspect", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if inspect.returncode != 0:
        run(["docker", "pull", IMAGE], check=False)

    proc = run([
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        f"--device", f"{slave_name}:{SERIAL_DEV}",
        "-p", f"127.0.0.1:{HOST_PORT}:3001",
        "-v", f"{CONFIG_PATH}:/etc/ser2net/ser2net.yaml:ro",
        IMAGE,
    ])
    if proc.returncode != 0:
        fail(f"docker run failed with {proc.returncode}")

    # Wait for port to be ready
    for i in range(90):
        try:
            s = socket.create_connection(("127.0.0.1", HOST_PORT), timeout=1)
            s.close()
            break
        except Exception:
            time.sleep(0.5)
            running = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME], stdout=subprocess.PIPE).stdout.strip()
            if running == b"false":
                fail("container exited before accepting connections")
    else:
        fail("port not ready after timeout")

    # Check non-root user
    proc = run(["docker", "exec", CONTAINER_NAME, "id", "-u"], stdout=subprocess.PIPE)
    uid = proc.stdout.strip()
    if uid != b"1000":
        fail(f"expected uid 1000, got {uid}")

    # Bidirectional round-trip
    s = socket.create_connection(("127.0.0.1", HOST_PORT), timeout=5)
    s.settimeout(5)
    payload1 = b"hello-from-tcp\n"
    s.sendall(payload1)
    time.sleep(0.2)
    data1 = os.read(master_fd, 1024)
    if payload1 not in data1:
        fail(f"TCP->PTY failed: sent {payload1}, got {data1}")

    payload2 = b"hello-from-pty\n"
    os.write(master_fd, payload2)
    time.sleep(0.2)
    data2 = s.recv(1024)
    if payload2 not in data2:
        fail(f"PTY->TCP failed: sent {payload2}, got {data2}")

    s.close()

    # Clean stop
    run(["docker", "stop", "-t", "10", CONTAINER_NAME], check=True)
    time.sleep(1)
    proc = run(["docker", "inspect", "-f", "{{.State.ExitCode}} {{.State.Status}}", CONTAINER_NAME], stdout=subprocess.PIPE)
    out = proc.stdout.strip()
    print(f"Container state after stop: {out}", flush=True)

    cleanup()

    # Negative test: no config mount -> should fail quickly
    proc = run([
        "docker", "run", "--rm",
        f"--device", f"{slave_name}:{SERIAL_DEV}",
        IMAGE,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if proc.returncode == 0:
        fail("container started without config but should have failed")
    print("Negative test passed (exited non-zero without config)", flush=True)

    try:
        os.close(master_fd)
    except Exception:
        pass

    print("ALL TESTS PASSED", flush=True)


if __name__ == "__main__":
    main()

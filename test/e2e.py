#!/usr/bin/env python3
"""End-to-end test for the ser2net image.

Requires Docker. Creates a real PTY pair on the host, hands one end to the
container as the serial device, and talks to the other end from here:

    test client --TCP--> container :3001 --ser2net--> PTY slave <--> PTY master --test client

The container gets `--privileged -v /dev:/dev`, mirroring how the Deployment in
the cluster exposes the serial device (hostPath /dev + privileged); a GitHub
runner has no /dev/ttyUSB*, and passing a single /dev/pts node with --device is
not reliable across the container's devpts mount.

Usage: python3 test/e2e.py [image]
"""

import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tty

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "ghcr.io/jenserat/ser2net:ci-test"
CONTAINER = "ser2net-e2e"
HOST_PORT = 13001
CONFIG_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ser2net.yaml")
CONNECT_TIMEOUT = 60
IO_TIMEOUT = 10


def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def cleanup(container=CONTAINER):
    run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dump(container=CONTAINER):
    print(f"--- docker logs {container} ---", flush=True)
    run(["docker", "logs", container], stdout=sys.stdout, stderr=sys.stdout)
    print(f"--- docker inspect {container} ---", flush=True)
    run(["docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", container],
        stdout=sys.stdout, stderr=sys.stdout)


def fail(msg):
    print(f"\nERROR: {msg}", flush=True)
    dump()
    cleanup()
    sys.exit(1)


def read_fd(fd, want, timeout=IO_TIMEOUT):
    """Accumulate from fd until `want` shows up or the deadline passes."""
    buf = b""
    deadline = time.time() + timeout
    while want not in buf:
        left = deadline - time.time()
        if left <= 0:
            break
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError as e:
            print(f"note: read from PTY failed: {e}", flush=True)
            break
        if not chunk:
            break
        buf += chunk
    return buf


def read_sock(sock, want, timeout=IO_TIMEOUT):
    buf = b""
    deadline = time.time() + timeout
    sock.settimeout(timeout)
    while want not in buf:
        left = deadline - time.time()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def main():
    cleanup()

    tmpdir = tempfile.mkdtemp(prefix="ser2net-e2e-")
    config = os.path.join(tmpdir, "ser2net.yaml")

    # Real PTY pair: master stays here, slave goes to the container.
    master_fd, slave_fd = os.openpty()
    tty.setraw(master_fd)
    slave = os.ttyname(slave_fd)
    os.chmod(slave, 0o666)  # container runs as uid 1000
    print(f"serial device: {slave}", flush=True)

    with open(CONFIG_TEMPLATE) as f:
        rendered = f.read().replace("__SERIAL_DEVICE__", slave)
    with open(config, "w") as f:
        f.write(rendered)

    if run(["docker", "image", "inspect", IMAGE],
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        run(["docker", "pull", IMAGE], check=True)

    proc = run([
        "docker", "run", "-d",
        "--name", CONTAINER,
        "--privileged",
        "-v", "/dev:/dev",
        "-p", f"127.0.0.1:{HOST_PORT}:3001",
        "-v", f"{config}:/etc/ser2net/ser2net.yaml:ro",
        IMAGE,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print(proc.stderr.decode(), flush=True)
        fail(f"docker run failed ({proc.returncode})")

    # 1. Container stays up and the accepter comes up.
    connected = False
    for _ in range(CONNECT_TIMEOUT * 2):
        running = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                      stdout=subprocess.PIPE).stdout.strip()
        if running != b"true":
            fail("container exited before accepting connections")
        try:
            socket.create_connection(("127.0.0.1", HOST_PORT), timeout=1).close()
            connected = True
            break
        except OSError:
            time.sleep(0.5)
    if not connected:
        fail(f"port {HOST_PORT} did not accept connections")

    # 2. Entrypoint contract: non-root.
    uid = run(["docker", "exec", CONTAINER, "id", "-u"],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if uid.returncode != 0 or uid.stdout.strip() != b"1000":
        fail(f"expected uid 1000, got {uid.stdout.strip()!r} (rc={uid.returncode})")

    # 3. TCP -> serial.
    sock = socket.create_connection(("127.0.0.1", HOST_PORT), timeout=IO_TIMEOUT)
    payload = b"tcp-to-serial\n"
    sock.sendall(payload)
    got = read_fd(master_fd, payload)
    if payload not in got:
        fail(f"TCP->serial: sent {payload!r}, saw {got!r}")

    # 4. serial -> TCP.
    payload = b"serial-to-tcp\n"
    os.write(master_fd, payload)
    got = read_sock(sock, payload)
    if payload not in got:
        fail(f"serial->TCP: sent {payload!r}, saw {got!r}")

    # 5. Larger payload survives without truncation (buffer/partial-write issues).
    big = bytes((i % 89) + 32 for i in range(4096))
    sock.sendall(big)
    got = read_fd(master_fd, big, timeout=20)
    if big not in got:
        fail(f"TCP->serial bulk: sent {len(big)} bytes, saw {len(got)}")

    big = bytes(((i * 7) % 89) + 32 for i in range(4096))
    os.write(master_fd, big)
    got = read_sock(sock, big, timeout=20)
    if big not in got:
        fail(f"serial->TCP bulk: sent {len(big)} bytes, saw {len(got)}")

    sock.close()

    # 6. It must still be alive after a client disconnected (a crash here would
    #    otherwise be misread as a slow shutdown below).
    running = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                  stdout=subprocess.PIPE).stdout.strip()
    if running != b"true":
        fail("container died after the client disconnected")

    print("--- container log before shutdown ---", flush=True)
    run(["docker", "logs", CONTAINER], stdout=sys.stdout, stderr=sys.stdout)

    # 7. Graceful shutdown on SIGTERM. The assertion is that ser2net stops by
    #    itself inside the grace period; a SIGKILL (137) means it ignored the
    #    signal. The exact code is ser2net's business (the Debian package exits
    #    1 here).
    run(["docker", "stop", "-t", "10", CONTAINER], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state = run(["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", CONTAINER],
                stdout=subprocess.PIPE).stdout.decode().split()
    if len(state) != 2 or state[0] != "exited":
        fail(f"container did not stop: {state}")
    if state[1] == "137":
        fail("SIGTERM was not handled; container had to be SIGKILLed")
    print(f"stopped on SIGTERM with exit code {state[1]}", flush=True)
    cleanup()

    # 8. Without the config mount it must fail fast, not idle.
    proc = run(["docker", "run", "--rm", "--privileged", "-v", "/dev:/dev", IMAGE],
               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if proc.returncode == 0:
        fail("container started without a config; it should have exited non-zero")
    print(f"no-config run exited {proc.returncode} as expected", flush=True)

    os.close(master_fd)
    os.close(slave_fd)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nALL TESTS PASSED", flush=True)


if __name__ == "__main__":
    main()

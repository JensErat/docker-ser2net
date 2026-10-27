# docker-ser2net

A minimal [ser2net](https://github.com/cminyard/ser2net) container image,
published to the GitHub Container Registry as `ghcr.io/jenserat/ser2net`.

ser2net exposes a local serial port (`/dev/ttyUSB*`, …) as a TCP endpoint so
that a client can use it over the network. This image is the serial side of the
openHAB serial-bridge setup: a `ser2net` Deployment pinned to the node that
physically owns a USB serial device, plus a `socat` sidecar on the openHAB side
that turns the TCP endpoint back into a local PTY.

## What is in the image

- Debian `bookworm-slim` with the distro `ser2net` package (4.x).
- A dedicated non-root `ser2net` user (uid/gid 1000), member of the `dialout`
  group.
- `ENTRYPOINT ["ser2net", "-d", "-c", "/etc/ser2net/ser2net.yaml"]`.

The configuration is **not** baked in. Mount it at `/etc/ser2net/ser2net.yaml`,
e.g. from a Kubernetes ConfigMap.

`-d` makes ser2net run in the foreground *and* log to stdout. Without it
ser2net writes to syslog, which does not exist in the container, so all errors
would be lost.

## Build

```sh
docker build -t ghcr.io/jenserat/ser2net .
```

CI builds and pushes `ghcr.io/jenserat/ser2net` on every push to `main` and on
`v*` tags (see `.github/workflows/build.yml`). It authenticates with the
workflow's built-in `GITHUB_TOKEN` (`packages: write`), so there are **no
repository secrets to configure**.

The package is public, so the cluster pulls it anonymously — no image pull
secret is needed.

## Run

```sh
docker run --rm \
  --device /dev/ttyUSB0 \
  -v "$PWD/ser2net.yaml:/etc/ser2net/ser2net.yaml:ro" \
  -p 3001:3001 \
  ghcr.io/jenserat/ser2net
```

In Kubernetes the serial device is reached through a `hostPath` mount of `/dev`
and the pod needs `privileged: true`: `/dev/ttyUSB*` is char major 188, which
is not in the default device-cgroup allowlist, and capabilities cannot change
that. The pinning and hardening live in the deployment manifests, not here.

## Notes

- The user is in `dialout`, but the container runtime does **not** apply the
  image's supplementary groups. If the pod ever runs without `privileged`, set
  `securityContext.supplementalGroups: [20]` on it (or use a device plugin).
- ser2net logs to stdout with `-d`; there is no syslog daemon in the image.

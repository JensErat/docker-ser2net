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

## Test

Run the end-to-end test locally (requires Docker):

```sh
docker build -t ghcr.io/jenserat/ser2net:ci-test .
python3 test/e2e.py ghcr.io/jenserat/ser2net:ci-test
```

The test creates a real PTY pair, hands the slave to the container as its serial
device and drives the other end. It asserts that the TCP↔serial path carries
data in both directions (including a 4 KiB payload), that the container runs as
uid 1000, that it survives a client disconnect, that it stops on SIGTERM rather
than having to be SIGKILLed, and that starting without a config fails fast
instead of idling. The container is run with `--privileged -v /dev:/dev` — the
same way the Deployment exposes a real `/dev/ttyUSB*` — because `--device` with a
single `/dev/pts` node does not survive the container's separate devpts mount.
`test/ser2net.yaml` is a template; the harness substitutes the PTY path and
mounts the rendered file.

In CI the `build` workflow runs this on every pull request and on `main`; the
`docker` job is the required status check on `main` (branch ruleset). Pull
requests build and test only — no image is pushed. On `main` and `v*` tags the
image is pushed after the tests pass.

## Releases

Releases are cut by the build itself; nothing is tagged by hand. A build becomes
a release when any of these hold:

- it was started manually (`workflow_dispatch`), or a `v*` tag was pushed;
- the ser2net package version inside the image differs from the one in the
  published `latest` — this is how a package-only update from the daily rebuild
  gets a release;
- the Debian base image digest differs from the digest recorded on the published
  `latest` (label `org.jenserat.base-digest`, stamped at build time) — this is
  how the daily rebuild picks up a new `bookworm-slim`;
- the push to `main` touched anything outside `.github/`, `test/`, `README.md`
  and `.gitignore`, i.e. the image inputs changed.

So a Dependabot **base image** bump produces a release when it lands, while a
Dependabot **GitHub Actions** bump does not.

The release tag is the immutable image tag
(`4.3.11-pkg.1.<build time>.<short sha>`), which is why several releases can
exist for the same ser2net version even though published releases on this
repository are immutable and their tags can never be reused. Release creation is
skipped on pull requests, so it can never block a merge.

CI builds and tests `ghcr.io/jenserat/ser2net` on every push to `main`, on `v*`
tags, on pull requests, and on a daily schedule (see `.github/workflows/build.yml`).
It authenticates with the workflow's built-in `GITHUB_TOKEN` (`packages: write`)
for push/schedule runs, so there are **no repository secrets to configure**.
On pull requests images are built and end-to-end tested but not pushed.

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

## The `Unable to start mdns: Out of memory` startup line

The container logs this once per start and then runs normally. It is **not** an
out-of-memory condition and is safe to ignore:

- ser2net initializes its optional mDNS backend unconditionally at startup
  (`gensio_alloc_mdns()` in `init_dataxfer()`), regardless of the config.
- In a minimal container there is no avahi/dbus, so gensio's mDNS backend cannot
  build its poll object and returns `GE_NOMEM`.
- `GE_NOMEM` is gensio error code **1**, whose error string is literally
  `"Out of memory"` — a generic allocation-failure label, not a measurement of
  the container's memory.
- The failure is non-fatal (`/* Not fatal */` in the source) and does not affect
  raw TCP serving. The port's own mDNS is off anyway (`mdns` defaults to
  `false`), so no service is advertised.

Setting `mdns: false` in the config does **not** silence it: the init runs before
per-port settings are applied. Raising the memory limit does not help either.
The only way to remove the line is to build ser2net/gensio without mDNS support,
which is not worth it for one cosmetic message. Observed with the Debian
`bookworm` package (ser2net 4.3.11); it is a gensio behaviour, not
distribution-specific.


FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install --no-install-recommends -y ser2net \
 && rm -rf /var/lib/apt/lists/*

# Dedicated non-root user in the dialout group (gid 20). Upstream recommends
# not running ser2net as root; dialout lets it open /dev/ttyUSB* (which is
# root:dialout 0660). This covers the *file-permission* layer only. The
# *device-cgroup* layer is why the pod still needs `privileged: true`:
# capabilities do not relax the device cgroup, so they cannot replace it.
RUN groupadd --gid 1000 ser2net \
 && useradd --uid 1000 --gid 1000 --groups dialout \
        --no-create-home --shell /usr/sbin/nologin ser2net

USER 1000:1000

# -d: run in the foreground AND send logs to stdout. ser2net otherwise logs to
# syslog, which does not exist inside the container. The config file is
# mounted from a ConfigMap at runtime; `/etc/ser2net` is the ser2net 4.x
# default sysconfdir.
ENTRYPOINT ["ser2net", "-d", "-c", "/etc/ser2net/ser2net.yaml"]

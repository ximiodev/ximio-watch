# Shared VPS demo deployment guide

This guide describes a **persistent demonstration deployment** of XimioWatch on an
existing shared VPS. It is not the final independent production deployment. The
stack uses fictional demo endpoints, loopback-only publishing and no external
notification delivery.

Do not connect to or modify any existing application, container, network,
firewall, Nginx, DNS, TLS, SSH or swap configuration on the host.

## Host prerequisites

- Ubuntu 22.04.5 LTS, x86_64.
- Docker 29.10.0 and Docker Compose v5.8.0 or compatible newer versions.
- At least 2 vCPU and 3.8 GiB total RAM.
- At least 1.7 GiB currently available RAM before starting this stack.
- The dedicated stack budget is 1024 MiB RAM and 0.95 vCPU by default.
- Keep at least 6 GiB free disk space after images and volumes are created.

Read-only preflight checks:

```bash
free -h
df -h /var/lib/docker
docker system df
docker ps --format 'table {{.Names}}\t{{.Status}}'
for port in 18100 18101 19090 13000 19093; do
  ss -ltn "( sport = :${port} )"
done
```

If any proposed port is already in use, change the corresponding
`SHARED_*_PORT` value in the private environment file. If free space after
deployment would drop below 6 GiB, stop and report the constraint rather than
pruning or deleting unrelated data.

Image disk use is dominated by the Python base images, Prometheus, Grafana and
Alertmanager. Pulling images and creating writeable layers plus TSDB/Grafana
volumes can consume several GiB over time; monitor `docker system df` and the
project volumes regularly.

Stage 5.1.1 measured the final linux/amd64 images locally: XimioWatch 57.95 MiB,
Demo API 57.81 MiB, Prometheus 110.71 MiB, Grafana 284.59 MiB and Alertmanager
42.16 MiB (553.22 MiB when summed). The two Python application images share
57.79 MiB of base layers, so the distinct layer footprint of all five images is
495.38 MiB. Fresh project volumes initially used about 60 KiB for Prometheus,
50 MiB for Grafana and 4 KiB for Alertmanager. Allowing for Docker metadata and
writeable layers, budget roughly 0.6 GiB initially. On a host with 9.1 GiB free,
that leaves about 8.5 GiB, or roughly 2.5 GiB above the 6 GiB minimum; TSDB and
Grafana growth can consume that reserve. These are local measurements, not VPS
measurements.

## Pinned runtime images

Use readable version tags with verified manifest digests. All four references
below resolve to official multi-platform manifests containing linux/amd64.

| Component | Pinned reference |
| --- | --- |
| Python application base | `python:3.12.16-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b` |
| Prometheus | `prom/prometheus:v3.16.0@sha256:e04987641b57704f408e31f6373ba4138ecf27cb4a165d24aabb6c38fddd0f06` |
| Grafana | `grafana/grafana:12.4.16@sha256:982fa6bf18cc38280a30bf08c3b673486067d4805b25a9d4756688a689a993c7` |
| Alertmanager | `prom/alertmanager:v0.35.1@sha256:4e3d029e69c7f61dd53a2b31a3dc427a2b81736cffca185c0854075e07969e36` |

To update an image later:

1. Choose a supported official stable release from the upstream project. Do not
   use `latest`, prerelease or unofficial replacement images.
2. Read the upstream release notes and security notes for breaking changes.
3. Verify the manifest digest, for example with
   `docker buildx imagetools inspect <repository>:<tag>@<digest>`, and confirm a
   linux/amd64 entry exists.
4. Scan the exact `tag@digest` reference with Trivy before changing the Compose
   files. Record the database snapshot date and compare like with like.
5. Replace the old reference everywhere: both Dockerfiles, standalone Compose,
   the Stage 4 overlay, shared-demo Compose, `.github/workflows/alerting.yml`,
   README examples and relevant documentation.
6. Rebuild both application images, rerun tests, config checks and the isolated
   five-service integration verification before using the new reference.

The scans at the time of this stage found no fixable critical vulnerabilities in
the pinned versions. High findings remain in Debian Python base packages, in
Alertmanager's embedded `golang.org/x/net`, and in two Grafana embedded Tempo
libraries. Findings without upstream fixes remain in the Python base image.
These are loopback-only demonstration services on a shared host, not a critical
production monitoring installation; do not expose them publicly. Trivy is a
snapshot-based scanner: a clean critical result is not a guarantee of absence
of unknown vulnerabilities or of safe exposure to untrusted input. Docker Scout
could not be used locally because the CLI required Docker account login; no
credentials were supplied.

The isolated verification confirmed Grafana login with the file-based password,
provisioning of the 12-panel dashboard and acceptance of all 19 panel expressions
by the Prometheus API. The available in-app browser did not expose rendered panel
canvases in its headless session, including for the previous Grafana version, so
visual panel rendering is not treated as verified. Confirm it visually through
SSH local forwarding during an authorized deployment. Local resource, health,
alert and persistence measurements do not guarantee identical VPS behaviour.

## Configuration and secrets

1. Copy the sanitized example:

   ```bash
   cp docs/shared-demo.env.example .env.shared-demo
   ```

2. Create a unique Grafana administrator password. `admin`/`admin` is not
   permitted: the Compose file requires both Grafana variables and reads the
   password from a file.

   ```bash
   mkdir -p secrets
   umask 027
   printf '%s' 'REPLACE_WITH_A_UNIQUE_LOCAL_PASSWORD' > secrets/grafana_admin_password
   chown root:root secrets/grafana_admin_password
   chmod 0440 secrets/grafana_admin_password
   ```

   Grafana 12.4.16 runs as UID 472 with primary GID 0. The mount is read-only and
   Group `0` can read a `0440 root:root` file. If a different permission scheme
   is used, the file must still be readable by UID 472. Never commit this file.

3. Set `SHARED_GRAFANA_ADMIN_USER` and `SHARED_GRAFANA_PASSWORD_FILE` in
   `.env.shared-demo`. The password file path is relative to the Compose file.

4. Optional: to override the fictional monitor configuration, place a
   sanitized YAML file on the host and set `SHARED_MONITOR_CONFIG` to its path.
   It is mounted read-only at `/app/config/monitors.runtime.yaml`. The
   XimioWatch container runs as `appuser` (UID 1000 in the supplied image), so
   the file must be readable by UID 1000. Do not bake private configuration into
   an image.

`.gitignore` excludes `.env.*` and `secrets/`; verify this before committing any
local work.

## Startup

Use the documented project name so volumes and networks remain project-scoped:

```bash
docker compose -p ximiowatch-demo \
  -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo \
  up -d --build
```

The Compose file also declares `name: ximiowatch-demo`, so `-p` is a safety
explicitness rather than a requirement.

Default loopback host ports:

| Service | Host port | Container port |
| --- | ---: | ---: |
| XimioWatch | 18100 | 8000 |
| Demo API | 18101 | 8001 |
| Prometheus | 19090 | 9090 |
| Grafana | 13000 | 3000 |
| Alertmanager | 19093 | 9093 |

All ports are published only on `127.0.0.1`. Internal traffic uses the
project-scoped `shared-demo` bridge network. There is no host networking,
privileged mode, Docker socket mount or external application network.

## Private access through SSH local forwarding

From a trusted local machine:

```bash
ssh -N -L 13000:127.0.0.1:13000 <SSH_USER>@<VPS>
ssh -N -L 19090:127.0.0.1:19090 <SSH_USER>@<VPS>
ssh -N -L 19093:127.0.0.1:19093 <SSH_USER>@<VPS>
ssh -N -L 18100:127.0.0.1:18100 <SSH_USER>@<VPS>
ssh -N -L 18101:127.0.0.1:18101 <SSH_USER>@<VPS>
```

Open Grafana at `http://localhost:13000` through the tunnel. Demo API
simulation endpoints remain local to the host and should only be reached over
the tunnel. Do not expose them through Nginx, DNS or a public firewall rule.
Grafana anonymous access and signup are disabled.

## Resource limits and inspection

Defaults, configurable through `SHARED_*` variables in the private env file:

| Service | Memory limit | Memory reservation | CPU limit | PID limit |
| --- | ---: | ---: | ---: | ---: |
| XimioWatch | 192 MiB | 128 MiB | 0.20 | 128 |
| Demo API | 96 MiB | 64 MiB | 0.10 | 64 |
| Prometheus | 320 MiB | 192 MiB | 0.25 | 256 |
| Grafana | 320 MiB | 192 MiB | 0.30 | 256 |
| Alertmanager | 96 MiB | 64 MiB | 0.10 | 64 |

Total default hard limit: 1024 MiB RAM and 0.95 vCPU. The same values are
declared in `deploy.resources.limits` and in the standalone Compose runtime keys
`mem_limit`, `mem_reservation`, `cpus` and `pids_limit`. Both groups are driven by
the same `SHARED_*` variables; the runtime keys make HostConfig enforcement
verifiable with `docker inspect` on non-Swarm Compose. No unrelated containers
are affected.

Verify enforced HostConfig values:

```bash
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo ps -q | while read -r id; do
    docker inspect --format '{{.Name}} memory={{.HostConfig.Memory}} nanocpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}}' "$id"
  done
docker stats --no-stream $(docker compose -p ximiowatch-demo \
  -f docker-compose.shared-demo.yml --env-file .env.shared-demo ps -q)
```

If changing limits, keep the sum at or below the available RAM budget. Do not
silently raise the default total. No limits are applied to unrelated containers.

## Persistence and health

Named volumes are project-scoped by Compose:

- `ximiowatch-demo_prometheus-data`
- `ximiowatch-demo_grafana-data`
- `ximiowatch-demo_alertmanager-data`

They preserve Prometheus history, Grafana state and Alertmanager silences
across container recreation. Do not use `down -v` during ordinary restarts.

Healthchecks use binaries verified in the selected images:

- Python services: `python` and `urllib.request`.
- Prometheus and Alertmanager: `/bin/wget`.
- Grafana: `/usr/bin/wget`.

```bash
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo ps
docker inspect --format '{{.Name}} {{.State.Health.Status}}' \
  $(docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
    --env-file .env.shared-demo ps -q)
```

## Prometheus and Alertmanager

`prometheus/prometheus.shared-demo.yml` uses a 15s scrape interval, 5s
evaluation interval, the shared recording rules and the fast demo alerts. It
does not load production thresholds. Edit that file to adjust intervals without
changing application code.

Retention flags are configurable in the private env file:

- `SHARED_PROMETHEUS_RETENTION_TIME=7d`
- `SHARED_PROMETHEUS_RETENTION_SIZE=512MB`

Retention by size is **not** a strict filesystem quota. WAL, compaction
temporaries and transient storage can exceed the configured value. Monitor disk
usage rather than treating the flag as a hard cap.

Alertmanager uses `alertmanager/alertmanager.shared-demo.yml`. Its receiver has
no notification integration, so alerts are recorded and silences persist, but
no external delivery occurs. Telegram remains inactive. Notification grouping
values are conservative demo values, not final production tuning.

## HTTP memory safety

The existing bounded HTTP implementation is retained. The bundled monitor
configuration targets only trusted, controlled local demo endpoints. A known
limitation remains: a compressed response chunk can expand before the cumulative
decoded-size check, so peak process memory is not strictly bounded by
`max_response_bytes`. Hardening compressed-response handling is a required
follow-up before monitoring arbitrary or untrusted endpoints. Do not claim the
container memory limit makes response-size validation a strict memory bound.

## Backup and restore

Create a backup directory on the host and back up only project volumes:

```bash
mkdir -p backups
docker run --rm \
  -v ximiowatch-demo_prometheus-data:/data \
  -v ximiowatch-demo_grafana-data:/grafana \
  -v ximiowatch-demo_alertmanager-data:/alertmanager \
  -v "$PWD/backups:/backup" \
  python:3.12.16-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b sh -c \
    'tar czf /backup/ximiowatch-demo-$(date +%Y%m%d).tgz -C / data grafana alertmanager'
```

Restore only after stopping the stack, and only into the matching project
volumes:

```bash
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo down
tar tzf backups/<backup-file>.tgz
docker run --rm \
  -v ximiowatch-demo_prometheus-data:/data \
  -v ximiowatch-demo_grafana-data:/grafana \
  -v ximiowatch-demo_alertmanager-data:/alertmanager \
  -v "$PWD/backups:/backup" \
  python:3.12.16-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b sh -c \
    'tar xzf /backup/<backup-file>.tgz -C /'
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo up -d
```

Never delete unrelated volumes or run global Docker pruning as part of this
procedure.

## Restart and safe shutdown

```bash
# Restart without deleting data:
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo restart

# Stop while preserving data, networks and volumes:
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo stop

# Remove containers/network but KEEP named volumes:
docker compose -p ximiowatch-demo -f docker-compose.shared-demo.yml \
  --env-file .env.shared-demo down
```

If memory pressure, excessive disk growth or repeated OOM events are observed,
stop the stack with `docker compose ... stop`, inspect `docker stats` and
`docker inspect --format '{{.State.OOMKilled}}'`, reduce load, and only restart
after the cause is understood. Do not change host swap, kill unrelated
containers or prune global Docker data.

## Migration to an independent VPS

Copy the repository, the private env file, the Grafana password file and the
three project-volume backups. Recreate the same Compose project name, verify
ports and resource limits on the new host, restore volumes before starting, and
then start with `up -d`. No application-code changes should be required. Adjust
host ports and resource variables for the new machine; keep all published ports
loopback-only until a separately authorized deployment design exists.

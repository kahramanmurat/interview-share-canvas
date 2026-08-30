#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${GRAFANA_PASSWORD_PARAMETER:?GRAFANA_PASSWORD_PARAMETER is required}"

APPLICATION_DIRECTORY="/opt/interview-share-canvas-observability"
COMPOSE_FILE="${APPLICATION_DIRECTORY}/observability/docker-compose.yaml"
COMPOSE_PROJECT="observability"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "${COMPOSE_FILE} is missing. The deploy payload did not unpack." >&2
  exit 1
fi

# Systems Manager reports Online early in boot, before cloud-init has finished
# running UserData, which is what installs Docker and mounts /data.
if ! cloud-init status --wait; then
  echo "cloud-init did not finish successfully; the host is not prepared." >&2
  exit 1
fi

systemctl enable --now docker
docker compose version >/dev/null

if ! mountpoint --quiet /data; then
  echo "/data is not mounted. The instance UserData prepares it; inspect the host." >&2
  exit 1
fi

# Each image runs as its own unprivileged user, and a bind mounted directory
# arrives owned by root, so every one of them needs its owner set before the
# container can write a byte.
prepare_directory() {
  mkdir -p "$1"
  chown "$2" "$1"
}
prepare_directory /data/prometheus 65534:65534
prepare_directory /data/loki 10001:10001
prepare_directory /data/tempo 10001:10001
prepare_directory /data/grafana 472:472

# Read by the host itself: a Systems Manager command and its parameters are
# readable in the console and in CloudTrail, so the password is never passed in.
GRAFANA_ADMIN_PASSWORD="$(aws ssm get-parameter \
  --region "${AWS_REGION}" \
  --name "${GRAFANA_PASSWORD_PARAMETER}" \
  --with-decryption \
  --query Parameter.Value \
  --output text)"
if [[ -z "${GRAFANA_ADMIN_PASSWORD}" || "${GRAFANA_ADMIN_PASSWORD}" == "None" ]]; then
  echo "${GRAFANA_PASSWORD_PARAMETER} is empty. Create it as a SecureString first." >&2
  exit 1
fi

cd "${APPLICATION_DIRECTORY}/observability"
GRAFANA_ANONYMOUS=false \
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD}" \
PROMETHEUS_DATA=/data/prometheus \
LOKI_DATA=/data/loki \
TEMPO_DATA=/data/tempo \
GRAFANA_DATA=/data/grafana \
docker compose --project-name "${COMPOSE_PROJECT}" up --detach --pull always --remove-orphans

docker compose --project-name "${COMPOSE_PROJECT}" ps --format '{{.Service}} {{.State}}'

# Nothing is reachable from outside the host, so the health check runs here.
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error http://127.0.0.1:3000/api/health >/dev/null \
    && curl --fail --silent --show-error http://127.0.0.1:9090/-/ready >/dev/null \
    && curl --fail --silent --show-error http://127.0.0.1:3200/ready >/dev/null \
    && curl --fail --silent --show-error http://127.0.0.1:3100/ready >/dev/null; then
    echo "Grafana, Prometheus, Tempo, and Loki are all ready."
    exit 0
  fi
  sleep 5
done

echo "The stack started but its services did not all become ready." >&2
docker compose --project-name "${COMPOSE_PROJECT}" ps
exit 1

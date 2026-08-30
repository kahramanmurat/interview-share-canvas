#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${IMAGE_URI:?IMAGE_URI is required}"
: "${PUBLIC_BASE_URL:?PUBLIC_BASE_URL is required}"
: "${ENVIRONMENT_NAME:?ENVIRONMENT_NAME is required}"
: "${IMAGE_TAG:?IMAGE_TAG is required}"

# Optional. Without a collector endpoint the backend runs uninstrumented.
OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-}"
OTLP_ENDPOINT_PARAMETER="${OTLP_ENDPOINT_PARAMETER:-/interview-share-canvas/observability/otlp-endpoint}"

# The observability stack publishes its collector address to this parameter, and
# rewrites it whenever its instance is replaced. Reading it here rather than
# baking the address into the pipeline is the entire coupling between the two
# stacks: no shared network, no cross-stack reference, and a private address
# that stays correct. An explicit endpoint still wins, and a missing parameter
# just means the observability stack is not deployed.
if [[ -z "${OTEL_EXPORTER_OTLP_ENDPOINT}" ]]; then
  OTEL_EXPORTER_OTLP_ENDPOINT="$(aws ssm get-parameter \
    --region "${AWS_REGION}" \
    --name "${OTLP_ENDPOINT_PARAMETER}" \
    --query Parameter.Value \
    --output text 2>/dev/null || true)"
  if [[ "${OTEL_EXPORTER_OTLP_ENDPOINT}" == "None" ]]; then
    OTEL_EXPORTER_OTLP_ENDPOINT=""
  fi
fi

if [[ -n "${OTEL_EXPORTER_OTLP_ENDPOINT}" ]]; then
  echo "Exporting telemetry to ${OTEL_EXPORTER_OTLP_ENDPOINT}"
else
  echo "No collector endpoint; the application will run uninstrumented."
fi

APPLICATION_DIRECTORY="/opt/interview-share-canvas"
COMPOSE_FILE="${APPLICATION_DIRECTORY}/docker-compose.yaml"
COMPOSE_PROJECT="interview-share-canvas"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "${COMPOSE_FILE} is missing. The deploy payload did not unpack." >&2
  exit 1
fi

# Systems Manager reports Online early in boot, before cloud-init has finished
# running UserData, which is what installs Docker and mounts /data. Without this
# wait the first deploy to a new instance fails with "Unit file docker.service
# does not exist".
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
mkdir -p /data/postgres

REGISTRY_HOST="${IMAGE_URI%%/*}"
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${REGISTRY_HOST}"

docker rm --force interview-share-canvas >/dev/null 2>&1 || true

cd "${APPLICATION_DIRECTORY}"
APP_IMAGE="${IMAGE_URI}" \
APP_PORT=80 \
POSTGRES_DATA=/data/postgres \
PUBLIC_BASE_URL="${PUBLIC_BASE_URL}" \
ENVIRONMENT_NAME="${ENVIRONMENT_NAME}" \
IMAGE_TAG="${IMAGE_TAG}" \
OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT}" \
RESTART_POLICY=unless-stopped \
docker compose --project-name "${COMPOSE_PROJECT}" up --detach --no-build --pull always --wait

docker compose --project-name "${COMPOSE_PROJECT}" ps --format '{{.Service}} {{.State}}'

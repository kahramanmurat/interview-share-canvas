#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${IMAGE_URI:?IMAGE_URI is required}"
: "${PUBLIC_BASE_URL:?PUBLIC_BASE_URL is required}"

APPLICATION_DIRECTORY="/opt/interview-share-canvas"
COMPOSE_FILE="${APPLICATION_DIRECTORY}/docker-compose.yaml"
COMPOSE_PROJECT="interview-share-canvas"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "${COMPOSE_FILE} is missing. The deploy payload did not unpack." >&2
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
RESTART_POLICY=unless-stopped \
docker compose --project-name "${COMPOSE_PROJECT}" up --detach --no-build --pull always --wait

docker compose --project-name "${COMPOSE_PROJECT}" ps --format '{{.Service}} {{.State}}'

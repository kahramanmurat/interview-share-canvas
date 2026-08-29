#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?AWS_REGION is required}"
: "${DATA_VOLUME_ID:?DATA_VOLUME_ID is required}"
: "${IMAGE_URI:?IMAGE_URI is required}"
: "${PUBLIC_BASE_URL:?PUBLIC_BASE_URL is required}"

systemctl enable --now docker

DATA_VOLUME_SERIAL="${DATA_VOLUME_ID//-/}"
DATA_DEVICE=""
for attempt in $(seq 1 60); do
  DATA_DEVICE="$(lsblk --nodeps --noheadings --output NAME,SERIAL | awk -v serial="${DATA_VOLUME_SERIAL}" '$2 == serial {print "/dev/" $1}')"
  if [[ -n "${DATA_DEVICE}" ]]; then
    break
  fi
  sleep 2
done
test -b "${DATA_DEVICE}"

if ! blkid "${DATA_DEVICE}" >/dev/null 2>&1; then
  mkfs.xfs "${DATA_DEVICE}"
fi
DATA_UUID="$(blkid -s UUID -o value "${DATA_DEVICE}")"
mkdir -p /data
if ! grep -q "UUID=${DATA_UUID}" /etc/fstab; then
  echo "UUID=${DATA_UUID} /data xfs defaults,nofail 0 2" >> /etc/fstab
fi
mountpoint --quiet /data || mount /data
chown -R 10001:10001 /data

REGISTRY_HOST="${IMAGE_URI%%/*}"
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${REGISTRY_HOST}"
docker pull "${IMAGE_URI}"
docker rm --force interview-share-canvas >/dev/null 2>&1 || true
docker run --detach \
  --name interview-share-canvas \
  --restart unless-stopped \
  --publish 80:8091 \
  --volume /data:/data:Z \
  --env "PUBLIC_BASE_URL=${PUBLIC_BASE_URL}" \
  "${IMAGE_URI}"

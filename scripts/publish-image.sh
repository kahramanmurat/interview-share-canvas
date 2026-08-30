#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"

AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
REPOSITORY_NAME="${REPOSITORY_NAME:-interview-share-canvas-app}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG is required, for example 20260818-163457-83242da}"

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY_HOST="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
REPOSITORY_URI="${REGISTRY_HOST}/${REPOSITORY_NAME}"

echo "Building and pushing ${REPOSITORY_URI}:${IMAGE_TAG}..."
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${REGISTRY_HOST}"
docker build --platform linux/amd64 --tag "${REPOSITORY_URI}:${IMAGE_TAG}" "${PROJECT_DIRECTORY}"
docker push "${REPOSITORY_URI}:${IMAGE_TAG}"

echo "${REPOSITORY_URI}:${IMAGE_TAG}"

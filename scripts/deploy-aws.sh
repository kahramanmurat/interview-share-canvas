#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
TEMPLATE_FILE="${PROJECT_DIRECTORY}/infrastructure/cloudformation.yaml"
REMOTE_DEPLOY_FILE="${PROJECT_DIRECTORY}/scripts/remote-deploy.sh"

AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-interview-share-canvas}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
ALLOWED_HTTP_CIDR="${ALLOWED_HTTP_CIDR:-0.0.0.0/0}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "${PROJECT_DIRECTORY}" rev-parse --short=12 HEAD)}"
CLOUDFORMATION_ROLE_ARN="${CLOUDFORMATION_ROLE_ARN:-}"

CLOUDFORMATION_ROLE_ARGUMENTS=()
if [[ -n "${CLOUDFORMATION_ROLE_ARN}" ]]; then
  CLOUDFORMATION_ROLE_ARGUMENTS=(--role-arn "${CLOUDFORMATION_ROLE_ARN}")
fi

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
VPC_ID="${VPC_ID:-$(aws ec2 describe-vpcs --region "${AWS_REGION}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)}"

if [[ -z "${VPC_ID}" || "${VPC_ID}" == "None" ]]; then
  echo "No default VPC found. Set VPC_ID and SUBNET_ID explicitly." >&2
  exit 1
fi

SUBNET_ID="${SUBNET_ID:-$(aws ec2 describe-subnets --region "${AWS_REGION}" --filters "Name=vpc-id,Values=${VPC_ID}" Name=map-public-ip-on-launch,Values=true --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text)}"
AVAILABILITY_ZONE="$(aws ec2 describe-subnets --region "${AWS_REGION}" --subnet-ids "${SUBNET_ID}" --query 'Subnets[0].AvailabilityZone' --output text)"

if ! aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" >/dev/null 2>&1; then
  echo "Creating the ECR repository with CloudFormation..."
  aws cloudformation deploy \
    --region "${AWS_REGION}" \
    --stack-name "${STACK_NAME}" \
    --template-file "${TEMPLATE_FILE}" \
    --capabilities CAPABILITY_IAM \
    "${CLOUDFORMATION_ROLE_ARGUMENTS[@]}" \
    --parameter-overrides \
      DeploymentMode=Bootstrap \
      VpcId="${VPC_ID}" \
      SubnetId="${SUBNET_ID}" \
      AvailabilityZone="${AVAILABILITY_ZONE}" \
    --no-fail-on-empty-changeset
fi

REPOSITORY_URI="$(aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue | [0]" --output text)"

echo "Building and pushing ${REPOSITORY_URI}:${IMAGE_TAG}..."
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker build --platform linux/amd64 --tag "${REPOSITORY_URI}:${IMAGE_TAG}" "${PROJECT_DIRECTORY}"
docker push "${REPOSITORY_URI}:${IMAGE_TAG}"

echo "Deploying the application host with CloudFormation..."
aws cloudformation deploy \
  --region "${AWS_REGION}" \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_IAM \
  "${CLOUDFORMATION_ROLE_ARGUMENTS[@]}" \
  --parameter-overrides \
    DeploymentMode=Deploy \
    ImageTag="${IMAGE_TAG}" \
    VpcId="${VPC_ID}" \
    SubnetId="${SUBNET_ID}" \
    AvailabilityZone="${AVAILABILITY_ZONE}" \
    InstanceType="${INSTANCE_TYPE}" \
    AllowedHttpCidr="${ALLOWED_HTTP_CIDR}" \
  --no-fail-on-empty-changeset

APPLICATION_URL="$(aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue | [0]" --output text)"
INSTANCE_ID="$(aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" --output text)"
DATA_VOLUME_ID="$(aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" --query "Stacks[0].Outputs[?OutputKey=='DataVolumeId'].OutputValue | [0]" --output text)"

echo "Waiting for Systems Manager on ${INSTANCE_ID}..."
for attempt in $(seq 1 60); do
  PING_STATUS="$(aws ssm describe-instance-information --region "${AWS_REGION}" --filters "Key=InstanceIds,Values=${INSTANCE_ID}" --query 'InstanceInformationList[0].PingStatus' --output text)"
  if [[ "${PING_STATUS}" == "Online" ]]; then
    break
  fi
  sleep 5
done
if [[ "${PING_STATUS:-}" != "Online" ]]; then
  echo "Systems Manager did not become ready on ${INSTANCE_ID}." >&2
  exit 1
fi

REMOTE_SCRIPT_BASE64="$(base64 < "${REMOTE_DEPLOY_FILE}" | tr -d '\n')"
REMOTE_COMMAND="echo ${REMOTE_SCRIPT_BASE64} | base64 --decode | AWS_REGION=${AWS_REGION} DATA_VOLUME_ID=${DATA_VOLUME_ID} IMAGE_URI=${REPOSITORY_URI}:${IMAGE_TAG} PUBLIC_BASE_URL=${APPLICATION_URL} bash"
COMMAND_ID="$(aws ssm send-command \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[\"${REMOTE_COMMAND}\"]}" \
  --query 'Command.CommandId' \
  --output text)"
aws ssm wait command-executed --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" || true
COMMAND_STATUS="$(aws ssm get-command-invocation --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" --query Status --output text)"
if [[ "${COMMAND_STATUS}" != "Success" ]]; then
  aws ssm get-command-invocation --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" --output json >&2
  exit 1
fi

echo "Waiting for ${APPLICATION_URL}/health..."
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error "${APPLICATION_URL}/health" >/dev/null; then
    echo "Deployment is healthy: ${APPLICATION_URL}"
    exit 0
  fi
  sleep 5
done

echo "The stack deployed, but its health endpoint did not become ready: ${APPLICATION_URL}/health" >&2
exit 1

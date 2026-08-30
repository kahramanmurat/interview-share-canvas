#!/usr/bin/env bash
set -euo pipefail

# Deploys the observability host: an OpenTelemetry Collector in front of
# Prometheus, Loki, Tempo, and Grafana. It is its own CloudFormation stack with
# its own instance and volume, and it is deployed by hand rather than by the
# pipeline, because it is infrastructure the application environments consume
# rather than something a code change should restart.

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
TEMPLATE_FILE="${PROJECT_DIRECTORY}/infrastructure/observability.yaml"

AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-interview-share-canvas-observability}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
DATA_VOLUME_SIZE="${DATA_VOLUME_SIZE:-20}"
GRAFANA_PASSWORD_PARAMETER="${GRAFANA_PASSWORD_PARAMETER:-/interview-share-canvas/observability/grafana-admin-password}"
OTLP_ENDPOINT_PARAMETER="${OTLP_ENDPOINT_PARAMETER:-/interview-share-canvas/observability/otlp-endpoint}"

VPC_ID="${VPC_ID:-$(aws ec2 describe-vpcs --region "${AWS_REGION}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)}"
if [[ -z "${VPC_ID}" || "${VPC_ID}" == "None" ]]; then
  echo "No default VPC found. Set VPC_ID and SUBNET_ID explicitly." >&2
  exit 1
fi

SUBNET_ID="${SUBNET_ID:-$(aws ec2 describe-subnets --region "${AWS_REGION}" --filters "Name=vpc-id,Values=${VPC_ID}" Name=map-public-ip-on-launch,Values=true --query 'sort_by(Subnets,&AvailabilityZone)[0].SubnetId' --output text)}"
AVAILABILITY_ZONE="$(aws ec2 describe-subnets --region "${AWS_REGION}" --subnet-ids "${SUBNET_ID}" --query 'Subnets[0].AvailabilityZone' --output text)"
# The application hosts live in this VPC, and nothing outside it should be able
# to write telemetry, so the VPC's own range is what the collector accepts.
ALLOWED_OTLP_CIDR="${ALLOWED_OTLP_CIDR:-$(aws ec2 describe-vpcs --region "${AWS_REGION}" --vpc-ids "${VPC_ID}" --query 'Vpcs[0].CidrBlock' --output text)}"

if ! aws ssm get-parameter --region "${AWS_REGION}" --name "${GRAFANA_PASSWORD_PARAMETER}" >/dev/null 2>&1; then
  echo "${GRAFANA_PASSWORD_PARAMETER} does not exist." >&2
  echo "The deployed Grafana requires a login and the host reads its password from" >&2
  echo "that parameter. Create it as a SecureString, then run this script again." >&2
  echo "See the Observability section of the README for the exact commands." >&2
  exit 1
fi

echo "Deploying the observability host with CloudFormation..."
aws cloudformation deploy \
  --region "${AWS_REGION}" \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    VpcId="${VPC_ID}" \
    SubnetId="${SUBNET_ID}" \
    AvailabilityZone="${AVAILABILITY_ZONE}" \
    AllowedOtlpCidr="${ALLOWED_OTLP_CIDR}" \
    InstanceType="${INSTANCE_TYPE}" \
    DataVolumeSize="${DATA_VOLUME_SIZE}" \
    GrafanaPasswordParameter="${GRAFANA_PASSWORD_PARAMETER}" \
    OtlpEndpointParameterName="${OTLP_ENDPOINT_PARAMETER}" \
  --no-fail-on-empty-changeset

stack_output() {
  aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

INSTANCE_ID="$(stack_output InstanceId)"
OTLP_ENDPOINT="$(stack_output OtlpEndpoint)"

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

# COPYFILE_DISABLE stops macOS tar from embedding AppleDouble ._ entries for
# files carrying extended attributes. macOS hides them again on extract, so the
# payload looks clean locally, while GNU tar on the host writes them as real
# files. Grafana then tries to parse ._dashboards.yaml as provisioning config
# and refuses to start.
DEPLOY_PAYLOAD_BASE64="$(COPYFILE_DISABLE=1 tar --no-xattrs -c -C "${PROJECT_DIRECTORY}" -f - scripts/observability-remote-deploy.sh observability | base64 | tr -d '\n')"
REMOTE_COMMAND="set -euo pipefail; rm -rf /opt/interview-share-canvas-observability/observability /opt/interview-share-canvas-observability/scripts /opt/interview-share-canvas-observability/._*; mkdir -p /opt/interview-share-canvas-observability; echo ${DEPLOY_PAYLOAD_BASE64} | base64 --decode | tar -x -C /opt/interview-share-canvas-observability -f -; AWS_REGION=${AWS_REGION} GRAFANA_PASSWORD_PARAMETER=${GRAFANA_PASSWORD_PARAMETER} bash /opt/interview-share-canvas-observability/scripts/observability-remote-deploy.sh"
COMMAND_ID="$(aws ssm send-command \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name AWS-RunShellScript \
  --parameters "{\"commands\":[\"${REMOTE_COMMAND}\"]}" \
  --query 'Command.CommandId' \
  --output text)"

# Polled rather than waited on, for the same reason the application deploy
# polls: the waiter gives up long before cloud-init finishes on a new instance.
echo "Waiting for deployment command ${COMMAND_ID}..."
COMMAND_STATUS="Pending"
for attempt in $(seq 1 180); do
  COMMAND_STATUS="$(aws ssm get-command-invocation --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" --query Status --output text 2>/dev/null || echo Pending)"
  case "${COMMAND_STATUS}" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
  sleep 5
done
if [[ "${COMMAND_STATUS}" != "Success" ]]; then
  # Only the tail: the image pull writes thousands of progress lines to stderr.
  echo "Deployment command ${COMMAND_STATUS}." >&2
  aws ssm get-command-invocation --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" --query StandardOutputContent --output text | tail -20 >&2
  aws ssm get-command-invocation --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" --query StandardErrorContent --output text | tail -5 >&2
  exit 1
fi

aws ssm get-command-invocation --region "${AWS_REGION}" --command-id "${COMMAND_ID}" --instance-id "${INSTANCE_ID}" --query StandardOutputContent --output text | tail -8

echo
echo "The observability stack is up."
echo "  Collector     ${OTLP_ENDPOINT}"
echo "  Published at  ${OTLP_ENDPOINT_PARAMETER}"
echo "  Instance      ${INSTANCE_ID}"
echo
echo "Application hosts read that parameter on their next deploy, so redeploy dev"
echo "and prod to connect them. Nothing here is exposed to the internet; reach"
echo "Grafana with a port forwarding session:"
echo
echo "  $(stack_output GrafanaTunnelCommand)"
echo
echo "then open http://127.0.0.1:3000 and sign in as admin."

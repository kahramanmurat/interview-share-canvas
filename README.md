# Interview Share Canvas

Collaborative system-design interview canvas with a FastAPI backend, a static
frontend, and SQLite or PostgreSQL persistence.

## Repository layout

| Path | Contents |
| --- | --- |
| `backend/` | FastAPI application and its pytest suite |
| `frontend/` | Static frontend, its `node build.mjs` build, and Node tests |
| `integration/` | API tests that run against a live Compose stack |
| `e2e/` | Playwright two-browser collaboration test |
| `observability/` | A separate Compose project: OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana, for the telemetry the backend exports |
| `infrastructure/` | CloudFormation: `registry.yaml` owns the shared ECR registry, `cloudformation.yaml` is one application environment, `github-oidc.yaml` is one environment's GitHub OIDC roles |
| `scripts/` | `deploy-aws.sh` and the `remote-deploy.sh` it runs on the host over SSM |
| `openapi.yaml` | Committed API contract, asserted against the live routes by the backend tests |
| `.github/workflows/` | The CI/CD pipeline |

## Run with Docker

Run these commands from the repository root.

Build the image:

```bash
docker build -t interview-share-canvas .
```

This single-container mode is for local use only. The AWS environments run
Docker Compose with PostgreSQL, not SQLite.

Start the application and mount the local `data` directory for SQLite
persistence:

```bash
mkdir -p data
docker run --rm \
  --name interview-share-canvas \
  -p 8091:8091 \
  --mount type=bind,source="$(pwd)/data",target=/data \
  interview-share-canvas
```

Open <http://localhost:8091> in a browser.

Stop the application with `Ctrl+C`. The container is removed automatically,
but its data remains in `data/interview-share-canvas.db`. Run the same
`docker run` command to start it again with the existing data.

Inspect the database locally with:

```bash
sqlite3 data/interview-share-canvas.db
```

## Run with PostgreSQL

Docker Compose starts the application and PostgreSQL together:

```bash
docker compose up --build -d
```

Open <http://localhost:8091>. PostgreSQL data is retained in the
`postgres-data` Docker volume.

Confirm both services are running:

```bash
docker compose ps
```

Confirm the backend is connected to PostgreSQL rather than SQLite:

```bash
docker compose exec app python -c 'from backend.main import store; print(store.engine.dialect.name); print(store.engine.url.render_as_string(hide_password=True))'
```

The first output line should be `postgresql`. Inspect the stored data with:

```bash
docker compose exec postgres psql -U interview -d interview_share_canvas
```

Then run these commands inside `psql`:

```sql
\conninfo
\dt
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM interview_sessions;
SELECT id, title, state, created_at
FROM interview_sessions
ORDER BY created_at DESC;
```

Create or update an interview at <http://localhost:8091>, then repeat the final
query to verify that PostgreSQL accepted the change. Exit `psql` with `\q`.

View application and database logs with:

```bash
docker compose logs -f app postgres
```

Stop the services with:

```bash
docker compose down
```

To also delete the PostgreSQL data and restore a fresh seeded database on the
next start, run `docker compose down --volumes`. This permanently deletes the
current Compose PostgreSQL data.

If an older standalone PostgreSQL container is using host port `5432`, find and
stop it before starting another host-exposed PostgreSQL container:

```bash
docker ps
docker stop interview-canvas-db
```

For an existing PostgreSQL server, set `DATABASE_URL` before starting the
backend:

```bash
export DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE"
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8091
```

## Run without Docker

Install dependencies and start the backend and frontend together:

```bash
uv sync
make run
```

Then open <http://localhost:8091>.

## Deploy to AWS with CloudFormation

### Environments

Two independent environments run on AWS. Each is its own CloudFormation stack
with its own EC2 host, encrypted EBS data volume, Elastic IP, and IAM roles.

| Environment | Stack | Reached at |
| --- | --- | --- |
| Development | `interview-share-canvas-dev` | <http://54.85.40.252> |
| Production | `interview-share-canvas-prod` | <http://34.205.14.31> |

Both share one image registry. The stack `interview-share-canvas-registry` owns
the ECR repository `interview-share-canvas-app`, which has immutable tags,
scan-on-push, and a lifecycle policy that keeps the 20 newest images.

Each environment also has an OIDC stack, `interview-share-canvas-dev-oidc` and
`interview-share-canvas-prod-oidc`, holding one deploy role and one
CloudFormation execution role.

The original single-environment stack `interview-share-canvas` still exists and
still answers on <http://98.87.32.42>. It is legacy and pending removal. It is
not one of the environments above; do not deploy to it.

### What actually runs on a host

Each host runs this repository's own `docker-compose.yaml`: the application
container plus `postgres:17-bookworm`. It is the same file, and therefore the
same stack, that the `compose-tests` CI job validates, so the stack CI tests is
the stack that ships.

- The PostgreSQL data directory is `/data/postgres` on the encrypted EBS
  volume. The volume is formatted and mounted by the instance `UserData`;
  nothing else formats it.
- The host overrides are `APP_IMAGE` (the ECR image and tag), `APP_PORT=80`,
  `POSTGRES_DATA=/data/postgres`, `RESTART_POLICY=unless-stopped`,
  `PUBLIC_BASE_URL`, and the telemetry variables `ENVIRONMENT_NAME`,
  `IMAGE_TAG`, and `OTEL_EXPORTER_OTLP_ENDPOINT` described under
  [Observability](#observability). Unset, every one of them falls back to the local
  development default, which is why one Compose file serves local development,
  CI, and both hosts.
- `UserData` prepares the host and stops there: Docker, the pinned Compose
  plugin, and the mounted volume. `scripts/remote-deploy.sh`, run over Systems
  Manager, is the only thing that starts the application. Because `ImageTag`
  does not appear in `UserData`, deploying a new image never replaces the
  instance.
- Amazon Linux 2023 does not package the Compose plugin, so `UserData`
  downloads Docker Compose v5.5.0 and verifies it against the release's
  published SHA-256 checksum before making it executable.

The stack intentionally exposes only port 80; administration is available with
AWS Systems Manager Session Manager instead of SSH.

### Inspect a running host

Open a Session Manager shell on the instance, then:

```bash
docker ps --format '{{.Names}} {{.Image}}'
docker exec interview-share-canvas-app-1 python -c "from backend.main import store; print(store.engine.dialect.name)"
```

The first command lists two containers, `interview-share-canvas-app-1` running
the ECR image and `interview-share-canvas-postgres-1` running
`postgres:17-bookworm`. The second prints `postgresql`. To confirm the data
directory on the EBS volume, `cat /data/postgres/PG_VERSION` prints `17`.

### Deploying by hand

`scripts/deploy-aws.sh` is the same script CI runs, so a local deploy and a
pipeline deploy produce the same result. Running it locally against the same
stack is a valid way to recover if the pipeline is unavailable.

Prerequisites:

- AWS CLI credentials with CloudFormation, EC2, EBS, ECR, IAM, and SSM access
- Docker running locally, when publishing an image
- A default VPC with at least one public subnet, or explicit `VPC_ID` and
  `SUBNET_ID` environment variables

Build, publish, and deploy to dev:

```bash
STACK_NAME=interview-share-canvas-dev \
ENVIRONMENT_NAME=dev \
./scripts/deploy-aws.sh
```

Promote an image that is already in the registry to production, without
building:

```bash
STACK_NAME=interview-share-canvas-prod \
ENVIRONMENT_NAME=prod \
PUBLISH_IMAGE=false \
IMAGE_TAG=<commit-sha> \
./scripts/deploy-aws.sh
```

The script deploys `infrastructure/cloudformation.yaml`, then uses Systems
Manager to run `scripts/remote-deploy.sh` on the host, which starts the Compose
stack idempotently. It waits for `/health` and prints the public URL.

Every variable the script reads:

| Variable | Default |
| --- | --- |
| `STACK_NAME` | Required. The environment's stack, for example `interview-share-canvas-dev` |
| `ENVIRONMENT_NAME` | Required. `dev` or `prod`. Names the environment's AWS resources |
| `REPOSITORY_NAME` | `interview-share-canvas-app`, the shared ECR repository |
| `PUBLISH_IMAGE` | `true`, which builds a Linux AMD64 image and pushes it. `false` promotes an existing tag and fails first if that tag is not in the repository |
| `AWS_REGION` | The AWS CLI's configured region, otherwise `us-east-1` |
| `INSTANCE_TYPE` | `t3.micro` |
| `ALLOWED_HTTP_CIDR` | `0.0.0.0/0` |
| `IMAGE_TAG` | Short commit SHA of `HEAD`. CI overrides this with the full SHA |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Unset, which deploys the backend uninstrumented. When set, the host exports telemetry to that collector |
| `CLOUDFORMATION_ROLE_ARN` | Unset. When set, CloudFormation applies the stack with that execution role instead of your own credentials. CI always sets it |
| `VPC_ID` | The account's default VPC |
| `SUBNET_ID` | First public subnet in that VPC, by availability zone |

View a deployed URL later with:

```bash
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name interview-share-canvas-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" \
  --output text
```

## Observability

The backend is instrumented with OpenTelemetry. Incoming requests, the
SQLAlchemy queries they run, and the application's logs are exported over OTLP
as traces, metrics, and logs.

Every span, metric, and log record carries the same three resource attributes,
which is what makes one telemetry stream from two identical environments
readable:

| Attribute | Value | Source |
| --- | --- | --- |
| `service.name` | `interview-share-canvas` | `OTEL_SERVICE_NAME`, otherwise the default |
| `deployment.environment.name` | `dev` or `prod` | `ENVIRONMENT_NAME`, the same value the deploy script names AWS resources with. `local` when nothing deployed the process |
| `service.version` | The deployed ECR image tag, for example `20260830-014229-4175fa9` | `IMAGE_TAG`, the same tag recorded on the instance as its `DeployedImageTag` tag. `unknown` when nothing deployed the process |

Telemetry is off unless `OTEL_EXPORTER_OTLP_ENDPOINT` names a collector, so
local runs, CI, and the test suite export nothing and pay for nothing. The
exporter speaks OTLP over HTTP and protobuf, appending `/v1/traces`,
`/v1/metrics`, and `/v1/logs` to that endpoint. Set `OTEL_SDK_DISABLED=true` to
turn a configured deployment off without changing its endpoint.

The `/health` endpoint is not traced. It is polled by the Compose healthcheck
and by the deploy script, and those spans would outnumber real traffic.

### Application metrics

Beyond the HTTP and database instrumentation, the backend records four metrics
of its own, in `backend/metrics.py`. None of them carries the environment or the
deployed version as an attribute: those are resource attributes on every signal
the process exports, so every metric below is already split by environment and
by version, and no call site has to remember to add them.

| Metric | Prometheus name | Kind | Attributes |
| --- | --- | --- | --- |
| Interview rooms created | `interview_rooms_created_total` | Counter | `room_source`: `new` or `duplicate` |
| Active interview participants | `interview_participants_active` | UpDownCounter | `participant_role`: `owner`, `candidate`, or `observer` |
| Canvas elements created | `canvas_elements_created_total` | Counter | `element_kind`: `node`, `edge`, or `stroke` |
| Failures creating canvas elements | `canvas_elements_creation_failures_total` | Counter | `failure_reason`: the error code returned, for example `editing_locked`, `document_too_large`, `validation_error` |

What each one counts, and why it counts that way:

- **Rooms** are counted where interviews are created through the API, so the
  seeded demo data is not counted. Every interview is one collaboration room.
- **Active participants** are those connected to a collaboration room right now,
  not everyone on the roster. The counter is incremented and decremented around
  the same `try`/`finally` that adds and removes the WebSocket, so a connection
  that fails before the room accepts it is never counted as present.
- **Elements created** is the rise in a document's element count. A canvas write
  replaces the whole document, so what was created is what the new document has
  beyond the old one, per kind. A deletion is not a negative creation, so a kind
  that shrank contributes nothing.
- **Creation failures** count canvas writes the server rejected, meaning the
  elements they carried were never created. They are recorded in the exception
  handlers rather than in the route, so every layer that can refuse a write is
  covered, including the request validation FastAPI does before the route runs.
  Rejected WebSocket document updates are counted the same way.

Grafana has a provisioned dashboard for exactly these four,
`observability/grafana/provisioning/dashboards/interview-share-canvas.json`,
which appears as **Interview Share Canvas** as soon as the stack starts. It has
two variables at the top, **Environment** and **Deployed version**, and every
panel filters on both, so the same dashboard shows one environment, one released
version, or everything at once. The version list narrows to the environments
selected, so picking `prod` leaves only the versions that have run in
production.

Four stat tiles read the current state, and four graphs read the trend: rooms
created per hour by source, participants connected by role, elements created per
minute by kind, and rejected writes per hour by reason.

The panels are Prometheus queries like any other. To write your own:

```promql
# Interviews started per hour, by environment
sum by (deployment_environment_name) (rate(interview_rooms_created_total[1h])) * 3600

# Who is in an interview right now
sum by (participant_role) (interview_participants_active)

# Share of canvas writes that failed, by reason, for one deployed version
sum by (failure_reason) (rate(canvas_elements_creation_failures_total{service_version="20260830-014229-4175fa9"}[5m]))
```

### The local observability stack

`observability/` is a second Compose project holding somewhere for that
telemetry to go: an OpenTelemetry Collector in front of Prometheus for metrics,
Loki for logs, and Tempo for traces, with Grafana already pointed at all three.

```bash
make observability
```

| Service | Address | Purpose |
| --- | --- | --- |
| Grafana | http://127.0.0.1:3000 | The **Interview Share Canvas** dashboard and ad hoc queries, anonymous admin, no login |
| Collector | `127.0.0.1:4318` (HTTP), `127.0.0.1:4317` (gRPC) | The only address the application needs |
| Prometheus | http://127.0.0.1:9090 | Metrics, 15 day retention |
| Loki | http://127.0.0.1:3100 | Logs, 7 day retention |
| Tempo | http://127.0.0.1:3200 | Traces, 7 day retention |

It is a separate Compose project on purpose. Either stack starts, stops, and is
rebuilt without the other: with no collector listening the application exports
nothing, and the collector is happy to sit idle. Nothing in `observability/` is
deployed to AWS.

The application reaches the collector over the host rather than over a shared
Compose network, so the endpoint differs by how the backend runs:

```bash
# Compose, which resolves the collector through the host gateway
ENVIRONMENT_NAME=local IMAGE_TAG=$(git rev-parse --short=7 HEAD) \
OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318 \
docker compose up --build

# uvicorn on the host
ENVIRONMENT_NAME=local IMAGE_TAG=$(git rev-parse --short=7 HEAD) \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
uv run uvicorn backend.main:app --port 8091
```

Then exercise the app and look in Grafana. `service.name`,
`deployment.environment.name`, and `service.version` survive the whole way
through, as Loki stream labels, as Prometheus labels, and on the spans
themselves, so every panel can be split by environment and by deployed version:

- **Traces**, Tempo: `{resource.deployment.environment.name = "local"}`
- **Logs**, Loki: `{service_name="interview-share-canvas", deployment_environment_name="local"}`
- **Metrics**, Prometheus: `http_server_duration_milliseconds_count{deployment_environment_name="local"}`

A log line links to the trace it was emitted inside, through the `trace_id` the
SDK records on every record, and Tempo generates span metrics and a service
graph into Prometheus, so request rate and latency per route come from the
traces without the application counting anything itself.

`make observability-down` stops the stack and keeps the collected telemetry in
its volumes. `docker compose -f observability/docker-compose.yaml down
--volumes` discards it.

Grafana runs with anonymous admin access, which is why every query port above
is published on the loopback address only. Do not put this stack on a routable
interface as it stands.

In AWS, the endpoint travels from the `OTEL_EXPORTER_OTLP_ENDPOINT` repository
or environment variable in GitHub, through `scripts/deploy-aws.sh`, into the
Compose stack on the host. Collector credentials deliberately do not travel
that path: a Systems Manager command and its parameters are readable in the
console and in CloudTrail. A collector that authenticates upstream should hold
its own credentials on the host, with the backend exporting to it unauthenticated
over localhost.

## Test

The suites below are the same ones CI runs, in the same order.

Run the backend tests. `backend/tests/test_openapi.py` checks the committed
`openapi.yaml` against the routes FastAPI actually registers, so that file must
stay in sync when routes change:

```bash
uv run pytest
```

Run the frontend build and Node tests. The `test` script builds first, so it
also catches build breakage:

```bash
npm --prefix frontend ci
npm --prefix frontend test
```

Run the API integration tests against a real Compose stack. They exercise the
HTTP API and PostgreSQL together rather than a test client:

```bash
APP_PORT=18091 docker compose -p interview-share-canvas-integration up --build --detach --wait
APPLICATION_URL=http://127.0.0.1:18091 node --test integration/stack.test.mjs
docker compose -p interview-share-canvas-integration down --volumes --remove-orphans
```

`APPLICATION_URL` defaults to `http://127.0.0.1:18091`. Point it at any running
deployment, including the AWS one, to check that instance instead.

Convenience targets are in the `Makefile`:

| Command | Runs |
| --- | --- |
| `make test` | Backend tests |
| `make check` | Lockfile verification and backend tests |
| `make e2e` | Playwright collaboration test, including browser install |

### End-to-end collaboration test

The Playwright test uses `docker-compose.yaml` to start an isolated application
and PostgreSQL stack on port `18091`. It creates separate interviewer and
candidate browser sessions, changes the candidate canvas, and verifies that the
interviewer receives the update.

Run the test from the repository root. The target installs the locked Playwright
dependencies and Chromium before starting the test:

```bash
make e2e
```

Alternatively, run the commands directly:

```bash
npm --prefix e2e ci
npm --prefix e2e run install:browsers
npm --prefix e2e test
```

The test uses the dedicated Compose project `interview-share-canvas-e2e`. Its
containers, network, and PostgreSQL volume are removed automatically after the
test, including when the test fails. Your regular stack and database are not
modified.

Two environment variables change that behavior:

| Variable | Effect |
| --- | --- |
| `E2E_REUSE_COMPOSE=1` | Skip starting and tearing down Compose. The harness waits for an already-running stack on port `18091` instead. CI uses this so the integration and browser suites share one stack. |
| `E2E_COMPOSE_PROJECT` | Use a different Compose project name. Defaults to `interview-share-canvas-e2e`. |

With `E2E_REUSE_COMPOSE=1` the harness will not clean up, because it did not
start the stack. Remove it yourself:

```bash
docker compose -p <project> down --volumes --remove-orphans
```

The application port is fixed at `18091` in `e2e/global-setup.js` and
`e2e/playwright.config.js`; change both together if you need a different one.

## CI/CD

`.github/workflows/ci-cd.yaml` runs on pull requests, pushes to `main`, and
manual dispatch. Each stage gates the next:

```
Backend tests  --+
                 +--> Compose integration --> Deploy to dev --> Deploy to production
Frontend tests --+       and E2E tests         (automatic)       (waits for approval)
```

1. **Backend tests** and **Frontend tests** run in parallel on separate runners.
2. **Compose integration and E2E tests** builds the Compose application and
   PostgreSQL services once on port `18091`, then runs both the API integration
   tests and the Playwright collaboration test against that single stack.
   Playwright reuses the already-running stack through `E2E_REUSE_COMPOSE=1`
   instead of starting a second one. Compose logs are always printed, browser
   traces and videos are uploaded as the `playwright-diagnostics` artifact on
   failure, and the stack is removed with its volume even when a test fails.
3. **Deploy to dev** is skipped on pull requests and needs no approval. It runs
   in the GitHub `development` environment, assumes that environment's deploy
   role through OIDC with no long-lived AWS keys, and runs
   `scripts/deploy-aws.sh` with `PUBLISH_IMAGE=true`. This is the only job that
   builds an image: it tags it with the full commit SHA and pushes it to the
   shared ECR repository, then deploys that tag to
   `interview-share-canvas-dev`.
4. **Deploy to production** runs in the GitHub `production` environment and
   waits for a required human reviewer before it starts. It deploys the exact
   same commit SHA tag to `interview-share-canvas-prod` with
   `PUBLISH_IMAGE=false`, so it builds nothing and verifies first that the tag
   is already in the repository. Production therefore runs the bytes dev ran.

The production deploy role holds no ECR write permission at all. It cannot even
call `ecr:GetAuthorizationToken`, so a promotion is structurally incapable of
publishing an image even if the pipeline asked it to. Neither environment's
role can touch the other environment's CloudFormation stack or run Systems
Manager commands on its instances.

The approval gate also gates AWS access. The OIDC token is minted only once the
job actually starts, so a run waiting for a reviewer holds no AWS credentials.

Each deploy applies `infrastructure/cloudformation.yaml` through that
environment's CloudFormation execution role, then swaps the running Compose
stack over Systems Manager. For an image-only change the EC2 instance is
updated in place rather than replaced, so the deploy is quick and the Elastic
IP and EBS data volume are untouched. Template changes that CloudFormation
cannot apply in place, such as a new `InstanceType` or a newer Amazon Linux AMI,
do replace the instance; the data volume and Elastic IP are separate resources
and survive that as well.

Each deployment is then verified twice: `scripts/deploy-aws.sh` polls `/health`
before it exits, and a separate `Validate deployment health` step re-queries
the stack's `ApplicationUrl` output and fails the job unless `/health` returns
`{"status":"ok"}`. A deploy that leaves the application unhealthy fails the
pipeline, and a failed dev deploy never reaches the production gate.

The ECR repository sets `ImageTagMutability: IMMUTABLE`, so a commit SHA tag
cannot be repointed at different image content once published. Ship a new commit
rather than rebuilding an existing tag.

### Bootstrapping a fresh AWS account

Each environment gets two roles, so that GitHub's federated identity never
holds general-purpose AWS administration rights:

- `<stack>-github-deploy` is the only role GitHub can assume. It can drive that
  one CloudFormation stack, read images from the shared ECR repository, and run
  `AWS-RunShellScript` on instances tagged into that stack. It cannot create IAM
  or EC2 resources directly, and it cannot reach the other environment.
- `<stack>-cloudformation` is assumed by CloudFormation itself and holds the
  permissions that actually create infrastructure. The deploy role may only pass
  it to CloudFormation, never assume it.

Only the environment that builds images gets ECR write permission, through the
`AllowImagePublish` parameter. Production is deployed with
`AllowImagePublish=false`.

Do these steps in order. Later steps depend on the outputs of earlier ones.

**1. Create the shared registry.** Both environments pull from it, so it must
exist before either environment does:

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-registry \
  --template-file infrastructure/registry.yaml
```

**2. Create the GitHub environments.** `production` needs a required reviewer
and a branch allow-list of exactly `main`:

```bash
gh api -X PUT repos/<owner>/<repo>/environments/development
gh api -X PUT repos/<owner>/<repo>/environments/production \
  -F "reviewers[][type]=User" \
  -F "reviewers[][id]=$(gh api user --jq .id)" \
  -F "deployment_branch_policy[protected_branches]=false" \
  -F "deployment_branch_policy[custom_branch_policies]=true"

gh api -X POST repos/<owner>/<repo>/environments/production/deployment-branch-policies \
  -f name=main -f type=branch
```

A custom branch policy is used rather than `protected_branches=true` because
the latter is vacuous when no branch protection exists.

**3. Discover the OIDC subject claims.** Run a throwaway `workflow_dispatch`
workflow with `id-token: write` in each environment that requests a token,
decodes it, and prints only the `sub` claim. The observed values have the form:

```
repo:<owner>@<owner-id>/<repo>@<repo-id>:environment:<development|production>
```

**The subject is discovered, never constructed.** GitHub documents the
immutable-ID subject and the environment subject separately and never shows
them combined, so the combined form cannot be derived from the documentation
with confidence. Guessing it is what broke the first pipeline run of this
repository. For that reason `infrastructure/github-oidc.yaml` takes
`OidcSubject` as a required parameter with no default: there is nothing to
guess wrong.

**4. Create the two OIDC stacks**, one per environment, passing the subject
observed for that environment:

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-dev-oidc \
  --template-file infrastructure/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ApplicationStackName=interview-share-canvas-dev \
    OidcSubject='<the development subject you observed>' \
    AllowImagePublish=true

aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-prod-oidc \
  --template-file infrastructure/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ApplicationStackName=interview-share-canvas-prod \
    OidcSubject='<the production subject you observed>' \
    AllowImagePublish=false
```

`CreateOidcProvider` defaults to `false`, which reuses an account-level GitHub
provider. Pass `true` on the first stack only when
`token.actions.githubusercontent.com` is not already registered in the AWS
account. Creating a second provider for the same URL fails.

**5. Set the GitHub Actions variables.** These are *variables*, not secrets.

| Repository variable | Value |
| --- | --- |
| `AWS_REGION` | The region you deployed into, for example `us-east-1` |
| `AWS_REPOSITORY_NAME` | The registry stack's `RepositoryName` output, `interview-share-canvas-app` |

| Per-environment variable | Value |
| --- | --- |
| `AWS_ROLE_ARN` | That environment's OIDC stack `GitHubRoleArn` output |
| `AWS_CLOUDFORMATION_ROLE_ARN` | That environment's OIDC stack `CloudFormationRoleArn` output |
| `AWS_STACK_NAME` | `interview-share-canvas-dev` or `interview-share-canvas-prod` |

Read the outputs with:

```bash
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name interview-share-canvas-dev-oidc \
  --query "Stacks[0].Outputs" --output table
```

Set them under Settings, Secrets and variables, Actions, or with
`gh variable set AWS_STACK_NAME --env development --body <value>`.

**6. Deploy dev, then prod**, either by pushing to `main` or by running
`scripts/deploy-aws.sh` as shown above. Dev must go first: production promotes
an image tag and refuses to run if that tag is not already in the registry.

### Trust boundary and forks

Each deploy role trusts one exact OIDC subject, bound to GitHub's numeric owner
and repository IDs rather than to names. The role keeps trusting this
repository even if it is renamed, and stops trusting the names if someone else
later claims them. Because the subject names the GitHub environment, a job
running in `development` cannot assume the production role and a job running in
`production` cannot assume the dev role.

A fork must discover its own subjects with the probe described above and pass
them as `OidcSubject`. There is no default to inherit.

To confirm the trust before running the pipeline:

```bash
aws iam get-role --role-name interview-share-canvas-prod-github-deploy \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals' \
  --output json
```

To confirm that the production role cannot publish images, list its policy
statements. `PublishApplicationImages` and `AuthenticateToEcr` must be absent:

```bash
aws iam get-role-policy \
  --role-name interview-share-canvas-prod-github-deploy \
  --policy-name DeployApplication \
  --query 'PolicyDocument.Statement[].Sid' --output text
```

## Known gaps

These are real and unaddressed. Read them before putting real interview content
into production.

1. **No TLS and no domain.** Both environments serve plain HTTP with an IP
   address for a hostname. A domain, a certificate, and an HTTPS endpoint are
   required before production carries real interview content.
2. **No database migrations.** `backend/store.py` calls
   `Base.metadata.create_all`, which creates missing tables but never alters
   existing ones. Now that both environments hold persistent PostgreSQL data,
   the first schema change that alters a column will silently fail to apply.
   Alembic is needed before the schema evolves.
3. **No production backups.** The data volume carries
   `DeletionPolicy: Snapshot`, which protects against stack deletion. It does
   not protect against data corruption or accidental deletion of rows. There is
   no scheduled backup.

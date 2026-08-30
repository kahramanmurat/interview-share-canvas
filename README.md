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
| `infrastructure/` | CloudFormation for the application stack and the GitHub OIDC roles |
| `scripts/` | `deploy-aws.sh` and the `remote-deploy.sh` it runs on the host over SSM |
| `openapi.yaml` | Committed API contract, asserted against the live routes by the backend tests |
| `.github/workflows/` | The CI/CD pipeline |

## Run with Docker

Run these commands from the repository root.

Build the image:

```bash
docker build -t interview-share-canvas .
```

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

The AWS deployment provisions an ECR repository, one Amazon Linux EC2 instance,
an encrypted EBS data volume, an Elastic IP, and IAM access for ECR and Systems
Manager. The EC2 instance runs the existing Docker image and persists the SQLite
database on the separate EBS volume.

Prerequisites:

- AWS CLI credentials with CloudFormation, EC2, EBS, ECR, IAM, and SSM access
- Docker running locally
- A default VPC with at least one public subnet, or explicit `VPC_ID` and
  `SUBNET_ID` environment variables

Deploy to the AWS CLI's configured region:

```bash
./scripts/deploy-aws.sh
```

The script builds a Linux AMD64 image, pushes it to ECR, deploys
`infrastructure/cloudformation.yaml`, uses Systems Manager to apply the selected
image idempotently, waits for `/health`, and prints the public URL. Optional
settings include:

```bash
AWS_REGION=us-east-1 \
STACK_NAME=interview-share-canvas \
INSTANCE_TYPE=t3.micro \
ALLOWED_HTTP_CIDR=0.0.0.0/0 \
./scripts/deploy-aws.sh
```

Every variable the script reads:

| Variable | Default |
| --- | --- |
| `AWS_REGION` | The AWS CLI's configured region, otherwise `us-east-1` |
| `STACK_NAME` | `interview-share-canvas` |
| `INSTANCE_TYPE` | `t3.micro` |
| `ALLOWED_HTTP_CIDR` | `0.0.0.0/0` |
| `IMAGE_TAG` | Short commit SHA of `HEAD`. CI overrides this with the full SHA |
| `CLOUDFORMATION_ROLE_ARN` | Unset. When set, CloudFormation applies the stack with that execution role instead of your own credentials. CI always sets it |
| `VPC_ID` | The account's default VPC |
| `SUBNET_ID` | First public subnet in that VPC, by availability zone |

Because the script is the same one CI runs, a local deploy and a pipeline deploy
produce the same result. Running it locally against the same stack is a valid
way to recover if the pipeline is unavailable.

The stack intentionally exposes only port 80; administration is available with
AWS Systems Manager Session Manager instead of SSH. The generated endpoint uses
HTTP. Add a domain, certificate, and HTTPS endpoint before using the application
for sensitive production interviews.

View the deployed URL later with:

```bash
aws cloudformation describe-stacks \
  --stack-name interview-share-canvas \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" \
  --output text
```

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
Backend tests  ─┐
                ├─> Compose integration and E2E tests ─> Deploy to AWS
Frontend tests ─┘                                        (main only)
```

1. **Backend tests** and **Frontend tests** run in parallel on separate runners.
2. **Compose integration and E2E tests** builds the Compose application and
   PostgreSQL services once on port `18091`, then runs both the API integration
   tests and the Playwright collaboration test against that single stack.
   Playwright reuses the already-running stack through `E2E_REUSE_COMPOSE=1`
   instead of starting a second one. Compose logs are always printed, browser
   traces and videos are uploaded as the `playwright-diagnostics` artifact on
   failure, and the stack is removed with its volume even when a test fails.
3. **Deploy to AWS** is skipped on pull requests. It assumes the deploy role
   through GitHub OIDC with no long-lived AWS keys, then runs
   `scripts/deploy-aws.sh`.

The deploy tags the image with the full commit SHA, pushes it to ECR, applies
`infrastructure/cloudformation.yaml` through the CloudFormation execution role,
and swaps the running container over Systems Manager. For an image-only change
the EC2 instance is updated in place rather than replaced, so the deploy is
quick and the Elastic IP and EBS data volume are untouched. Template changes
that CloudFormation cannot apply in place, such as a new `InstanceType` or a
newer Amazon Linux AMI, do replace the instance; the data volume and Elastic IP
are separate resources and survive that as well.

The deployment is then verified twice: `scripts/deploy-aws.sh` polls
`/health` before it exits, and a separate `Validate deployment health` step
re-queries the stack's `ApplicationUrl` output and fails the job unless
`/health` returns `{"status":"ok"}`. A deploy that publishes an image but leaves
the application unhealthy fails the pipeline.

The ECR repository sets `ImageTagMutability: IMMUTABLE`, so a commit SHA tag
cannot be repointed at different image content once published. Ship a new commit
rather than rebuilding an existing tag.

### Bootstrapping the AWS roles

The pipeline uses two roles so that GitHub's federated identity never holds
general-purpose AWS administration rights:

- `interview-share-canvas-github-deploy` is the only role GitHub can assume. It
  can push to this one ECR repository, drive this one CloudFormation stack, and
  run `AWS-RunShellScript` on instances tagged into that stack. It cannot create
  IAM or EC2 resources directly.
- `interview-share-canvas-cloudformation` is assumed by CloudFormation itself and
  holds the permissions that actually create infrastructure. The deploy role may
  only pass it to CloudFormation, never assume it.

The roles are bootstrapped once, with credentials that can create IAM roles:

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-github-oidc \
  --template-file infrastructure/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CreateOidcProvider=false
```

`CreateOidcProvider=false` reuses an account-level GitHub provider. Use `true`
only when `token.actions.githubusercontent.com` is not already registered in the
AWS account. Creating a second provider for the same URL fails.

Then read the role ARNs out of the stack and set them as GitHub Actions
repository variables:

```bash
aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name interview-share-canvas-github-oidc \
  --query "Stacks[0].Outputs" --output table
```

| Repository variable | Source |
| --- | --- |
| `AWS_REGION` | The region you deployed into, for example `us-east-1` |
| `AWS_ROLE_ARN` | `GitHubRoleArn` output |
| `AWS_CLOUDFORMATION_ROLE_ARN` | `CloudFormationRoleArn` output |

These are repository *variables*, not secrets. Set them under Settings, Secrets
and variables, Actions, Variables, or with
`gh variable set AWS_ROLE_ARN --body <arn>`.

### Trust boundary and forks

The deploy role trusts one exact OIDC subject:

```
repo:<owner>@<owner-id>/<repo>@<repo-id>:ref:refs/heads/main
```

Binding to GitHub's numeric owner and repository IDs, rather than to names,
means the role keeps trusting this repository even if it is renamed, and stops
trusting the names if someone else later claims them. Two consequences:

- Only pushes to `main` can assume the role. A `workflow_dispatch` run on any
  other branch reaches the deploy job and then fails at the credential step,
  by design.
- **The template defaults are this repository's IDs.** A fork must override
  them, or GitHub Actions will fail to assume the role. Look up your own IDs and
  pass all of them:

```bash
gh api users/<owner> --jq .id
gh api repos/<owner>/<repo> --jq .id

aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-github-oidc \
  --template-file infrastructure/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    CreateOidcProvider=false \
    GitHubOrganization=<owner> \
    GitHubOrganizationId=<owner-id> \
    GitHubRepository=<repo> \
    GitHubRepositoryId=<repo-id> \
    GitHubBranch=main \
    ApplicationStackName=interview-share-canvas
```

Use `gh api orgs/<org> --jq .id` instead of `users/<owner>` when the repository
belongs to an organization.

To confirm the trust before running the pipeline:

```bash
aws iam get-role --role-name interview-share-canvas-github-deploy \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals' \
  --output json
```

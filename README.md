# Interview Share Canvas

Collaborative system-design interview canvas with a FastAPI backend, a static
frontend, and SQLite or PostgreSQL persistence.

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

Run the backend tests:

```bash
uv run pytest
```

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

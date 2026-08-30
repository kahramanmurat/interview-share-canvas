# Dev and Production Environments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single AWS deployment into an automatically deployed `dev` environment and a manually approved `prod` environment that runs the exact image `dev` validated.

**Architecture:** One shared ECR repository owned by its own CloudFormation stack, plus two identical application stacks and two independent pairs of IAM roles. The pipeline builds and pushes once during the dev deploy, then promotes that same image tag to prod behind a GitHub Environment approval gate.

Each host runs the repository's own `docker-compose.yaml`: the application container plus `postgres:17-bookworm`, with the Postgres data directory on the EBS volume at `/data/postgres`. The stack CI validates is the stack that ships. `UserData` prepares the host only; `scripts/remote-deploy.sh`, run over Systems Manager, is the single path that starts the application.

**Tech Stack:** AWS CloudFormation, ECR, EC2, EBS, Systems Manager, IAM with GitHub OIDC federation, GitHub Actions, Bash, Docker, Docker Compose, PostgreSQL.

**Spec:** `_docs/2026-08-30-dev-prod-environments-design.md`

## Global Constraints

- AWS account `963656558707`, region `us-east-1`. No AWS Organization exists.
- GitHub repository `kahramanmurat/interview-share-canvas`, owner ID `1132768`, repository ID `1350826572`.
- GitHub environment names are `development` and `production`. AWS resource name suffixes are `dev` and `prod`. `EnvironmentName` parameters always take the AWS form.
- Stack names: `interview-share-canvas-registry`, `interview-share-canvas-dev`, `interview-share-canvas-prod`, `interview-share-canvas-dev-oidc`, `interview-share-canvas-prod-oidc`.
- The shared ECR repository is named `interview-share-canvas-app`. The existing `interview-share-canvas` repository is retired, not reused.
- Every GitHub Actions marketplace action stays pinned to its current immutable commit SHA. Do not change or "update" the pins.
- No IAM policy may be written against a guessed OIDC subject. Subjects come only from Task 2's observed output.
- The prod deploy role must never hold an ECR write permission.
- Backend, frontend, integration, and E2E suites must stay green throughout.
- One `docker-compose.yaml` serves local development, CI, and both hosts. There is no override file and no second compose file.
- Amazon Linux 2023 does not package the Docker Compose plugin. It is installed in `UserData` from a pinned, checksum-verified release binary. Never execute an unverified download.
- The EBS filesystem logic (`mkfs.xfs`, `blkid`, `/etc/fstab`) lives in exactly one place: the instance `UserData`.
- No emojis, no em dashes, in any file this plan creates.

**Refinement from the spec:** the spec described adding `RepositoryUri` and `RepositoryArn` parameters to the application template. This plan instead passes a single `RepositoryName` parameter and derives both the URI and the ARN inside the template with `!Sub`. Same result, one parameter instead of two, and the two values cannot drift apart.

---

### Task 1: Shared image registry stack

Creates the ECR repository that both environments will share. It is created under a new name so nothing existing has to be deleted before the new environments are proven.

**Files:**
- Create: `infrastructure/registry.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: CloudFormation stack `interview-share-canvas-registry` with outputs `RepositoryName` (string, `interview-share-canvas-app`), `RepositoryUri`, and `RepositoryArn`. Every later task refers to the repository by the `RepositoryName` output.

- [ ] **Step 1: Write the registry template**

Create `infrastructure/registry.yaml`:

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Description: Shared container image registry for all Interview Share Canvas environments.

Parameters:
  RepositoryName:
    Type: String
    Default: interview-share-canvas-app
    AllowedPattern: "^[a-z0-9][a-z0-9._/-]{1,255}$"

Resources:
  ContainerRepository:
    Type: AWS::ECR::Repository
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      RepositoryName: !Ref RepositoryName
      ImageScanningConfiguration:
        ScanOnPush: true
      ImageTagMutability: IMMUTABLE
      LifecyclePolicy:
        LifecyclePolicyText: |
          {
            "rules": [
              {
                "rulePriority": 1,
                "description": "Keep the 20 newest images",
                "selection": {
                  "tagStatus": "any",
                  "countType": "imageCountMoreThan",
                  "countNumber": 20
                },
                "action": {"type": "expire"}
              }
            ]
          }

Outputs:
  RepositoryName:
    Description: Repository name shared by every environment.
    Value: !Ref ContainerRepository
  RepositoryUri:
    Value: !GetAtt ContainerRepository.RepositoryUri
  RepositoryArn:
    Value: !GetAtt ContainerRepository.Arn
```

- [ ] **Step 2: Validate the template before deploying anything**

```bash
aws cloudformation validate-template \
  --region us-east-1 \
  --template-body file://infrastructure/registry.yaml
```

Expected: JSON describing one parameter, `RepositoryName`. Any error here is a syntax problem; fix before continuing.

- [ ] **Step 3: Deploy the registry stack**

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-registry \
  --template-file infrastructure/registry.yaml \
  --no-fail-on-empty-changeset
```

Expected: `Successfully created/updated stack - interview-share-canvas-registry`.

- [ ] **Step 4: Verify the repository exists and the old one is untouched**

```bash
aws cloudformation describe-stacks --region us-east-1 \
  --stack-name interview-share-canvas-registry \
  --query 'Stacks[0].Outputs' --output table

aws ecr describe-repositories --region us-east-1 \
  --query 'repositories[].repositoryName' --output json
```

Expected: the outputs table lists `interview-share-canvas-app`, and the repository list contains **both** `interview-share-canvas-app` and the original `interview-share-canvas`. If the original is missing, stop: something deleted it and the old environment can no longer deploy.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/registry.yaml
git commit -m "Add shared container registry stack"
```

---

### Task 2: GitHub environments and OIDC subject discovery

Creates the two GitHub environments and discovers the **actual** OIDC subject claim each one produces. Nothing in this plan may assume the subject format. GitHub documents the immutable-ID subject and the environment subject separately and never shows them combined, and an incorrect subject is what caused the first pipeline run of this repository to fail.

**Files:**
- Create, then delete before the task ends: `.github/workflows/oidc-subject-probe.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: two literal strings recorded in the plan checklist below, referred to later as `<DEV_SUBJECT>` and `<PROD_SUBJECT>`. Task 3 consumes them as the `OidcSubject` template parameter.

**Correction applied 2026-08-30.** The original instruction set
`deployment_branch_policy[protected_branches]=true`. That was verified to be
vacuous here: `main` is not a protected branch and the repository has zero
rulesets, yet a production deployment from `main` was still allowed. Because the
environment subject carries no branch component, the reviewer rule would have
been the only control. The environment now uses an explicit custom branch
policy allowing only `main`, which does not depend on branch protection
existing. Use the corrected commands below.

- [ ] **Step 1: Create the two GitHub environments**

```bash
gh api -X PUT repos/kahramanmurat/interview-share-canvas/environments/development
gh api -X PUT repos/kahramanmurat/interview-share-canvas/environments/production \
  -F "reviewers[][type]=User" \
  -F "reviewers[][id]=$(gh api user --jq .id)" \
  -F "deployment_branch_policy[protected_branches]=false" \
  -F "deployment_branch_policy[custom_branch_policies]=true"

gh api -X POST repos/kahramanmurat/interview-share-canvas/environments/production/deployment-branch-policies \
  -f name=main -f type=branch
```

Verify the allow-list contains exactly `main`:

```bash
gh api repos/kahramanmurat/interview-share-canvas/environments/production/deployment-branch-policies \
  --jq '{count: .total_count, branches: [.branch_policies[].name]}'
```

Expected: `{"count": 1, "branches": ["main"]}`.

Expected: two JSON objects. Verify the production one shows a `required_reviewers` protection rule:

```bash
gh api repos/kahramanmurat/interview-share-canvas/environments/production \
  --jq '.protection_rules[].type'
```

Expected output includes `required_reviewers`. If it does not, the approval gate does not exist and the rest of this plan is unsafe. Stop and fix.

- [ ] **Step 2: Write the throwaway subject probe workflow**

This prints only the `sub` claim. It must never print the token itself.

Create `.github/workflows/oidc-subject-probe.yaml`:

```yaml
name: OIDC subject probe

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  probe-development:
    runs-on: ubuntu-latest
    environment: development
    permissions:
      contents: read
      id-token: write
    steps:
      - name: Print only the subject claim
        run: |
          set -euo pipefail
          token=$(curl -sS \
            -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com" \
            | jq -r '.value')
          payload=$(printf '%s' "$token" | cut -d. -f2)
          padding=$(( (4 - ${#payload} % 4) % 4 ))
          if [ "$padding" -ne 0 ]; then
            payload="$payload$(printf '=%.0s' $(seq 1 "$padding"))"
          fi
          subject=$(printf '%s' "$payload" | tr '_-' '/+' | base64 -d | jq -r '.sub')
          echo "environment=development"
          echo "subject=${subject}"

  probe-production:
    runs-on: ubuntu-latest
    environment: production
    permissions:
      contents: read
      id-token: write
    steps:
      - name: Print only the subject claim
        run: |
          set -euo pipefail
          token=$(curl -sS \
            -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com" \
            | jq -r '.value')
          payload=$(printf '%s' "$token" | cut -d. -f2)
          padding=$(( (4 - ${#payload} % 4) % 4 ))
          if [ "$padding" -ne 0 ]; then
            payload="$payload$(printf '=%.0s' $(seq 1 "$padding"))"
          fi
          subject=$(printf '%s' "$payload" | tr '_-' '/+' | base64 -d | jq -r '.sub')
          echo "environment=production"
          echo "subject=${subject}"
```

Two explicit jobs rather than a matrix: `environment:` accepting a `matrix`
value is an avoidable risk in a file that exists only to run once.

- [ ] **Step 3: Commit and push the probe**

```bash
git add .github/workflows/oidc-subject-probe.yaml
git commit -m "Add temporary OIDC subject probe"
git push origin main
```

Note: pushing to `main` also triggers the existing CI/CD workflow, which will deploy the current single environment as usual. That is expected and harmless.

- [ ] **Step 4: Run the probe and approve the production leg**

```bash
gh workflow run oidc-subject-probe.yaml --ref main
```

The `probe-production` job will pause for review. Approve it in the Actions UI, or:

```bash
run_id=$(gh run list --workflow oidc-subject-probe.yaml --limit 1 --json databaseId --jq '.[0].databaseId')
gh api -X POST "repos/kahramanmurat/interview-share-canvas/actions/runs/$run_id/pending_deployments" \
  -F "environment_ids[]=$(gh api repos/kahramanmurat/interview-share-canvas/environments/production --jq .id)" \
  -f state=approved -f comment="subject probe"
```

The fact that it pauses at all is the first proof the approval gate works. Record that it paused.

- [ ] **Step 5: Record both subjects**

```bash
gh run view "$run_id" --log | grep -E "environment=|subject=" | sed 's/^.*\t//'
```

Write the two observed values here before continuing. Do not proceed with a blank:

```
DEV_SUBJECT  = repo:kahramanmurat@1132768/interview-share-canvas@1350826572:environment:development
PROD_SUBJECT = repo:kahramanmurat@1132768/interview-share-canvas@1350826572:environment:production
```

OBSERVED 2026-08-30 from live tokens with audience `sts.amazonaws.com`. The
immutable-ID prefix does compose with `:environment:<name>`, which GitHub's
documentation never shows. These are the literal values Task 3 must use.

Expected shape, to sanity-check rather than to assume: something ending in `:environment:development` and `:environment:production`. If the prefix is **not** the immutable `repo:kahramanmurat@1132768/interview-share-canvas@1350826572` form, that is important information, not a problem. Use exactly what was printed.

- [ ] **Step 6: Delete the probe workflow**

```bash
git rm .github/workflows/oidc-subject-probe.yaml
git commit -m "Remove temporary OIDC subject probe"
```

Do not push yet. Task 3 pushes nothing either; the next push happens in Task 10.

---

### Task 3: Per-environment IAM roles

Rewrites the OIDC template so it is deployed once per environment, binding each role to the subject observed in Task 2 and denying the prod role any ability to publish an image.

**Files:**
- Modify: `infrastructure/github-oidc.yaml` (full rewrite)

**Interfaces:**
- Consumes: `<DEV_SUBJECT>` and `<PROD_SUBJECT>` from Task 2; `RepositoryName` output from Task 1.
- Produces: stacks `interview-share-canvas-dev-oidc` and `interview-share-canvas-prod-oidc`, each with outputs `GitHubRoleArn` and `CloudFormationRoleArn`. Roles are named `<ApplicationStackName>-github-deploy` and `<ApplicationStackName>-cloudformation`.

- [ ] **Step 1: Replace `infrastructure/github-oidc.yaml`**

```yaml
AWSTemplateFormatVersion: "2010-09-09"
Description: GitHub Actions OIDC and CloudFormation execution roles for one Interview Share Canvas environment.

Parameters:
  ApplicationStackName:
    Type: String
    Description: The application stack this environment deploys, for example interview-share-canvas-dev.
  OidcSubject:
    Type: String
    Description: The exact sub claim observed from the subject probe. Never construct this by hand.
  RepositoryName:
    Type: String
    Default: interview-share-canvas-app
  AllowImagePublish:
    Type: String
    Default: "false"
    AllowedValues: ["true", "false"]
    Description: Only the environment that builds images may publish them.
  CreateOidcProvider:
    Type: String
    Default: "false"
    AllowedValues: ["true", "false"]
    Description: Set true only if this AWS account has no token.actions.githubusercontent.com provider.

Conditions:
  ShouldCreateOidcProvider: !Equals [!Ref CreateOidcProvider, "true"]
  CanPublishImages: !Equals [!Ref AllowImagePublish, "true"]

Resources:
  GitHubOidcProvider:
    Type: AWS::IAM::OIDCProvider
    Condition: ShouldCreateOidcProvider
    Properties:
      Url: https://token.actions.githubusercontent.com
      ClientIdList:
        - sts.amazonaws.com
      ThumbprintList:
        - 1b511abead59c6ce207077c0bf0e0043b1382612

  CloudFormationExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub ${ApplicationStackName}-cloudformation
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: cloudformation.amazonaws.com
            Action: sts:AssumeRole
      Policies:
        - PolicyName: ManageApplicationInfrastructure
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Sid: ManageEc2Infrastructure
                Effect: Allow
                Action: ec2:*
                Resource: "*"
              - Sid: ReadAmiParameters
                Effect: Allow
                Action:
                  - ssm:GetParameter
                  - ssm:GetParameters
                Resource: !Sub arn:${AWS::Partition}:ssm:${AWS::Region}::parameter/aws/service/ami-amazon-linux-latest/*
              - Sid: ManageApplicationRoles
                Effect: Allow
                Action:
                  - iam:AddRoleToInstanceProfile
                  - iam:AttachRolePolicy
                  - iam:CreateInstanceProfile
                  - iam:CreateRole
                  - iam:DeleteInstanceProfile
                  - iam:DeleteRole
                  - iam:DeleteRolePolicy
                  - iam:DetachRolePolicy
                  - iam:GetInstanceProfile
                  - iam:GetRole
                  - iam:GetRolePolicy
                  - iam:PassRole
                  - iam:PutRolePolicy
                  - iam:RemoveRoleFromInstanceProfile
                  - iam:TagInstanceProfile
                  - iam:TagRole
                  - iam:UntagInstanceProfile
                  - iam:UntagRole
                  - iam:UpdateAssumeRolePolicy
                Resource:
                  - !Sub arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${ApplicationStackName}-*
                  - !Sub arn:${AWS::Partition}:iam::${AWS::AccountId}:instance-profile/${ApplicationStackName}-*

  GitHubDeployRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub ${ApplicationStackName}-github-deploy
      MaxSessionDuration: 3600
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Federated: !If
                - ShouldCreateOidcProvider
                - !Ref GitHubOidcProvider
                - !Sub arn:${AWS::Partition}:iam::${AWS::AccountId}:oidc-provider/token.actions.githubusercontent.com
            Action: sts:AssumeRoleWithWebIdentity
            Condition:
              StringEquals:
                token.actions.githubusercontent.com:aud: sts.amazonaws.com
                token.actions.githubusercontent.com:sub: !Ref OidcSubject
      Policies:
        - PolicyName: DeployApplication
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Sid: UseCloudFormationStack
                Effect: Allow
                Action:
                  - cloudformation:CreateChangeSet
                  - cloudformation:DeleteChangeSet
                  - cloudformation:DescribeChangeSet
                  - cloudformation:DescribeStackEvents
                  - cloudformation:DescribeStacks
                  - cloudformation:ExecuteChangeSet
                  - cloudformation:GetTemplate
                  - cloudformation:GetTemplateSummary
                Resource: !Sub arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${ApplicationStackName}/*
              - Sid: PassCloudFormationExecutionRole
                Effect: Allow
                Action: iam:PassRole
                Resource: !GetAtt CloudFormationExecutionRole.Arn
                Condition:
                  StringEquals:
                    iam:PassedToService: cloudformation.amazonaws.com
              - Sid: ReadApplicationImages
                Effect: Allow
                Action: ecr:DescribeImages
                Resource: !Sub arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/${RepositoryName}
              - !If
                - CanPublishImages
                - Sid: PublishApplicationImages
                  Effect: Allow
                  Action:
                    - ecr:BatchCheckLayerAvailability
                    - ecr:BatchGetImage
                    - ecr:CompleteLayerUpload
                    - ecr:DescribeRepositories
                    - ecr:GetDownloadUrlForLayer
                    - ecr:InitiateLayerUpload
                    - ecr:PutImage
                    - ecr:UploadLayerPart
                  Resource: !Sub arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/${RepositoryName}
                - !Ref AWS::NoValue
              - !If
                - CanPublishImages
                - Sid: AuthenticateToEcr
                  Effect: Allow
                  Action: ecr:GetAuthorizationToken
                  Resource: "*"
                - !Ref AWS::NoValue
              - Sid: DiscoverNetwork
                Effect: Allow
                Action:
                  - ec2:DescribeSubnets
                  - ec2:DescribeVpcs
                Resource: "*"
              - Sid: TrackSystemsManagerCommands
                Effect: Allow
                Action:
                  - ssm:DescribeInstanceInformation
                  - ssm:GetCommandInvocation
                Resource: "*"
              - Sid: RunDeploymentOnApplicationInstances
                Effect: Allow
                Action: ssm:SendCommand
                Resource: !Sub arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/*
                Condition:
                  StringEquals:
                    ssm:resourceTag/aws:cloudformation:stack-name: !Ref ApplicationStackName
              - Sid: RunDeploymentThroughRunShellScript
                Effect: Allow
                Action: ssm:SendCommand
                Resource: !Sub arn:${AWS::Partition}:ssm:${AWS::Region}::document/AWS-RunShellScript

Outputs:
  GitHubRoleArn:
    Value: !GetAtt GitHubDeployRole.Arn
  CloudFormationRoleArn:
    Value: !GetAtt CloudFormationExecutionRole.Arn
```

- [ ] **Step 2: Validate the template**

```bash
aws cloudformation validate-template \
  --region us-east-1 \
  --template-body file://infrastructure/github-oidc.yaml
```

Expected: JSON listing parameters `ApplicationStackName`, `OidcSubject`, `RepositoryName`, `AllowImagePublish`, `CreateOidcProvider`.

- [ ] **Step 3: Deploy the dev roles**

Substitute the recorded `<DEV_SUBJECT>` literally.

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-dev-oidc \
  --template-file infrastructure/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ApplicationStackName=interview-share-canvas-dev \
    OidcSubject='<DEV_SUBJECT>' \
    RepositoryName=interview-share-canvas-app \
    AllowImagePublish=true \
    CreateOidcProvider=false \
  --no-fail-on-empty-changeset
```

- [ ] **Step 4: Deploy the prod roles**

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name interview-share-canvas-prod-oidc \
  --template-file infrastructure/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ApplicationStackName=interview-share-canvas-prod \
    OidcSubject='<PROD_SUBJECT>' \
    RepositoryName=interview-share-canvas-app \
    AllowImagePublish=false \
    CreateOidcProvider=false \
  --no-fail-on-empty-changeset
```

- [ ] **Step 5: Prove the prod role cannot publish images**

This is the security assertion of the whole task. It must fail closed.

```bash
PROD_ROLE=arn:aws:iam::963656558707:role/interview-share-canvas-prod-github-deploy
REPO_ARN=arn:aws:ecr:us-east-1:963656558707:repository/interview-share-canvas-app

aws iam simulate-principal-policy --policy-source-arn "$PROD_ROLE" \
  --action-names ecr:PutImage ecr:InitiateLayerUpload ecr:UploadLayerPart ecr:GetAuthorizationToken \
  --resource-arns "$REPO_ARN" \
  --query 'EvaluationResults[].{A:EvalActionName,D:EvalDecision}' --output table
```

Expected: every decision is `implicitDeny`. If any says `allowed`, stop and fix the template before going further.

- [ ] **Step 6: Prove neither role can touch the other environment's stack**

```bash
DEV_ROLE=arn:aws:iam::963656558707:role/interview-share-canvas-dev-github-deploy
PROD_ROLE=arn:aws:iam::963656558707:role/interview-share-canvas-prod-github-deploy

aws iam simulate-principal-policy --policy-source-arn "$DEV_ROLE" \
  --action-names cloudformation:CreateChangeSet \
  --resource-arns "arn:aws:cloudformation:us-east-1:963656558707:stack/interview-share-canvas-prod/*" \
  --query 'EvaluationResults[].EvalDecision' --output text

aws iam simulate-principal-policy --policy-source-arn "$PROD_ROLE" \
  --action-names cloudformation:CreateChangeSet \
  --resource-arns "arn:aws:cloudformation:us-east-1:963656558707:stack/interview-share-canvas-dev/*" \
  --query 'EvaluationResults[].EvalDecision' --output text
```

Expected: `implicitDeny` from both.

- [ ] **Step 7: Prove the dev role can still do its job**

```bash
aws iam simulate-principal-policy --policy-source-arn "$DEV_ROLE" \
  --action-names ecr:PutImage \
  --resource-arns "$REPO_ARN" \
  --query 'EvaluationResults[].EvalDecision' --output text

aws iam simulate-principal-policy --policy-source-arn "$DEV_ROLE" \
  --action-names cloudformation:CreateChangeSet cloudformation:GetTemplateSummary \
  --resource-arns "arn:aws:cloudformation:us-east-1:963656558707:stack/interview-share-canvas-dev/*" \
  --query 'EvaluationResults[].{A:EvalActionName,D:EvalDecision}' --output table
```

Expected: `allowed` for all three.

- [ ] **Step 8: Set the per-environment GitHub variables**

```bash
gh variable set AWS_REGION --body us-east-1
gh variable set AWS_REPOSITORY_NAME --body interview-share-canvas-app

for env in development:dev production:prod; do
  gh_env="${env%%:*}"; aws_env="${env##*:}"
  gh variable set AWS_STACK_NAME --env "$gh_env" --body "interview-share-canvas-$aws_env"
  gh variable set AWS_ROLE_ARN --env "$gh_env" \
    --body "$(aws cloudformation describe-stacks --region us-east-1 \
      --stack-name "interview-share-canvas-$aws_env-oidc" \
      --query "Stacks[0].Outputs[?OutputKey=='GitHubRoleArn'].OutputValue | [0]" --output text)"
  gh variable set AWS_CLOUDFORMATION_ROLE_ARN --env "$gh_env" \
    --body "$(aws cloudformation describe-stacks --region us-east-1 \
      --stack-name "interview-share-canvas-$aws_env-oidc" \
      --query "Stacks[0].Outputs[?OutputKey=='CloudFormationRoleArn'].OutputValue | [0]" --output text)"
done

gh variable list
gh variable list --env development
gh variable list --env production
```

Expected: repository-level `AWS_REGION` and `AWS_REPOSITORY_NAME`; each environment has its own three variables pointing at its own stack.

- [ ] **Step 9: Commit**

```bash
git add infrastructure/github-oidc.yaml
git commit -m "Scope OIDC roles to one environment each"
```

---

### Task 4: Parameterise the Compose stack for the host

Makes one `docker-compose.yaml` serve local development, CI, and the EC2 host. The
host will run this exact file, so the stack CI validates is the stack that ships.
Nothing about local or CI behaviour may change: every default is chosen so that an
unset variable reproduces today's file exactly.

**Files:**
- Modify: `docker-compose.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: a compose file driven by four optional variables. `APP_IMAGE` (default `interview-share-canvas:local`), `POSTGRES_DATA` (default the named volume `postgres-data`; an absolute path bind-mounts instead), `RESTART_POLICY` (default `no`), and the pass-through `PUBLIC_BASE_URL`, which reaches the container only when it is set in the environment. `APP_PORT` keeps its existing default of `8091`. Task 5 consumes all of these.

- [ ] **Step 1: Rewrite `docker-compose.yaml`**

Replace the whole file with:

```yaml
services:
  app:
    build: .
    image: ${APP_IMAGE:-interview-share-canvas:local}
    restart: ${RESTART_POLICY:-no}
    ports:
      - "${APP_PORT:-8091}:8091"
    environment:
      - DATABASE_URL=postgresql+psycopg://interview:interview@postgres:5432/interview_share_canvas
      - PUBLIC_BASE_URL
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: postgres:17-bookworm
    restart: ${RESTART_POLICY:-no}
    environment:
      POSTGRES_DB: interview_share_canvas
      POSTGRES_USER: interview
      POSTGRES_PASSWORD: interview
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U interview -d interview_share_canvas"]
      interval: 5s
      timeout: 5s
      retries: 10
    volumes:
      - ${POSTGRES_DATA:-postgres-data}:/var/lib/postgresql/data

volumes:
  postgres-data:
```

Four things about this file that are easy to get wrong:

1. The `app` service's `environment:` had to change from mapping form to list
   form in full. Compose does not allow one service to mix `KEY: value` mapping
   entries with bare `- KEY` pass-through entries under the same
   `environment:` key. `postgres` keeps mapping form because it needs no
   pass-through.
2. `- PUBLIC_BASE_URL` with no `=` means "pass this through from the host
   environment if it is set, otherwise do not define it in the container". That
   is why local and CI behaviour does not change: neither sets it.
3. `build: .` stays. `image:` next to `build:` names the image that a build
   produces, and names the image that `--no-build` pulls instead. That is the
   single mechanism that lets the host run a published ECR image from this
   file.
4. The `volumes:` top-level `postgres-data` declaration stays. When
   `POSTGRES_DATA` is an absolute path, Compose reads the mount as a bind mount
   and the declared named volume is simply unused, which is not an error.

- [ ] **Step 2: Prove the defaults reproduce today's behaviour**

`docker compose config` resolves the file without starting anything.

```bash
docker compose config --format json \
  | python3 -c "import json,sys; s=json.load(sys.stdin)['services']; v=s['postgres']['volumes'][0]; print(s['app']['image'], s['app']['restart'], v['type'], v['source'], s['app']['environment']['PUBLIC_BASE_URL'] is None)"
```

Expected exactly: `interview-share-canvas:local no volume postgres-data True`.

The last field is `is None` rather than a membership test on purpose. A
pass-through entry always appears in the resolved config; when the variable is
unset its value is `null`, and Compose omits a `null` variable when it creates
the container. Step 6 proves that at runtime rather than taking it on trust.

- [ ] **Step 3: Prove the host overrides do what the host needs**

```bash
APP_IMAGE=963656558707.dkr.ecr.us-east-1.amazonaws.com/interview-share-canvas-app:abc123 \
POSTGRES_DATA=/data/postgres \
RESTART_POLICY=unless-stopped \
APP_PORT=80 \
PUBLIC_BASE_URL=http://203.0.113.10 \
docker compose config --format json \
  | python3 -c "import json,sys; s=json.load(sys.stdin)['services']; v=s['postgres']['volumes'][0]; print(s['app']['image'], s['app']['restart'], v['type'], v['source'], s['app']['ports'][0]['published'], s['app']['environment']['PUBLIC_BASE_URL'])"
```

Expected exactly:
`963656558707.dkr.ecr.us-east-1.amazonaws.com/interview-share-canvas-app:abc123 unless-stopped bind /data/postgres 80 http://203.0.113.10`.

The `bind` in that output is the whole point of Step 1's `${POSTGRES_DATA:-postgres-data}`: a path substitutes for the named volume with no override file.

- [ ] **Step 4: Backend tests**

```bash
uv run pytest
```

Expected: all tests pass, exit code 0. These do not touch Compose; they are here to prove the working tree is clean before the Compose runs below.

- [ ] **Step 5: Frontend tests**

```bash
npm --prefix frontend ci && npm --prefix frontend test
```

Expected: all tests pass, exit code 0.

- [ ] **Step 6: Bring up the Compose stack the way CI does**

Use a dedicated project name so this cannot collide with a stack left running from ordinary development.

```bash
npm --prefix e2e ci
npm --prefix e2e exec -- playwright install --with-deps chromium
APP_PORT=18091 docker compose -p interview-share-canvas-plan up --build --detach --wait
```

Expected: both `app` and `postgres` reach a healthy or running state and the command exits 0. Confirm two containers:

```bash
APP_PORT=18091 docker compose -p interview-share-canvas-plan ps --format '{{.Service}} {{.State}}'
```

Expected two lines, `app running` and `postgres running`.

Now prove `PUBLIC_BASE_URL` really is absent inside the container, because
nothing set it:

```bash
APP_PORT=18091 docker compose -p interview-share-canvas-plan exec -T app printenv PUBLIC_BASE_URL; echo "exit=$?"
```

Expected: no value printed and `exit=1`. `printenv` exits 1 for a variable that
is not defined, which is exactly the pre-change behaviour this task must
preserve.

Shell note: this project is developed under zsh, where `status` is a read-only
builtin variable. Do not name a shell variable `status` in any snippet in this
plan or in any script it produces; `zsh` fails with `read-only variable: status`
and the failure looks like a Compose problem when it is not. Use
`container_state`, `command_state`, or similar.

- [ ] **Step 7: Integration tests against the running stack**

```bash
APPLICATION_URL=http://127.0.0.1:18091 node --test integration/stack.test.mjs
```

Expected: `# pass 1`, `# fail 0`. This is the test that proves the application is
talking to PostgreSQL through the Compose network, so it is the direct evidence
that the parameterisation did not break the data path.

- [ ] **Step 8: End-to-end tests against the same running stack**

```bash
E2E_REUSE_COMPOSE=1 E2E_COMPOSE_PROJECT=interview-share-canvas-plan npm --prefix e2e test
```

Expected: every Playwright test passes. `E2E_REUSE_COMPOSE=1` makes
`e2e/global-setup.js` reuse the stack from Step 6 rather than starting its own,
exactly as the `compose-tests` CI job does.

- [ ] **Step 9: Tear the stack down**

```bash
APP_PORT=18091 docker compose -p interview-share-canvas-plan down --volumes --remove-orphans
APP_PORT=18091 docker compose -p interview-share-canvas-plan ps --all --format '{{.Service}}'
```

Expected: the second command prints nothing. `--volumes` removes the test
database so the next run starts clean.

- [ ] **Step 10: Commit**

```bash
git add docker-compose.yaml
git commit -m "Parameterise the Compose stack for local, CI, and host use"
```

---

### Task 5: Deploy the application with Compose over SSM

Replaces the single `docker run` on the host with the Compose stack from Task 4,
and makes `scripts/deploy-aws.sh` ship the compose file alongside the remote
script. This is also where the duplicated EBS filesystem logic is removed from
`remote-deploy.sh`, leaving it in `UserData` only.

**Files:**
- Modify: `scripts/remote-deploy.sh` (full rewrite)
- Modify: `scripts/deploy-aws.sh` (payload and remote command only)

**Interfaces:**
- Consumes: the compose variables produced by Task 4.
- Produces: `scripts/remote-deploy.sh` driven by `AWS_REGION`, `IMAGE_URI`, and `PUBLIC_BASE_URL`. `DATA_VOLUME_ID` is no longer an input, because the script no longer touches the filesystem layer. `scripts/deploy-aws.sh` sends a tar payload containing both `scripts/remote-deploy.sh` and `docker-compose.yaml`, unpacked into `/opt/interview-share-canvas/` on the host. Task 6 relies on this by removing application startup from `UserData`.

- [ ] **Step 1: Rewrite `scripts/remote-deploy.sh`**

Replace the whole file with:

```bash
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
```

Five decisions worth stating, because each one is load-bearing:

1. All `mkfs.xfs`, `blkid`, and `/etc/fstab` handling is gone. It lives only in
   `UserData` after Task 6. This script asserts the mount instead of recreating
   it, so a genuinely unmounted `/data` fails loudly rather than being silently
   re-formatted.
2. `chown -R 10001:10001 /data` is gone and must not come back. That uid is the
   application user, but the directory is now PostgreSQL's. The `postgres` image
   entrypoint starts as root and sets ownership of its own data directory before
   dropping privileges, so `mkdir -p /data/postgres` is all the host owes it.
   Chowning `/data` to `10001` after Task 6 would break PostgreSQL startup.
3. `--no-build` means Compose never resolves `build: .`, so the absent build
   context on the host is not a problem. It also guarantees the host runs the
   published image rather than building its own.
4. `docker rm --force interview-share-canvas` removes the container left behind
   by the previous single-container scheme. It is a no-op on a fresh host and on
   every subsequent deploy.
5. `--wait` makes the command block until both services report healthy, so a
   failed database start fails the SSM command rather than surfacing later as a
   failed health check.

- [ ] **Step 2: Ship the compose file in the deploy payload**

In `scripts/deploy-aws.sh`, replace the two lines that build the base64 script
payload and the remote command:

```bash
REMOTE_SCRIPT_BASE64="$(base64 < "${REMOTE_DEPLOY_FILE}" | tr -d '\n')"
REMOTE_COMMAND="echo ${REMOTE_SCRIPT_BASE64} | base64 --decode | AWS_REGION=${AWS_REGION} DATA_VOLUME_ID=${DATA_VOLUME_ID} IMAGE_URI=${REPOSITORY_URI}:${IMAGE_TAG} PUBLIC_BASE_URL=${APPLICATION_URL} bash"
```

with:

```bash
DEPLOY_PAYLOAD_BASE64="$(tar -c -C "${PROJECT_DIRECTORY}" -f - scripts/remote-deploy.sh docker-compose.yaml | base64 | tr -d '\n')"
REMOTE_COMMAND="set -euo pipefail; mkdir -p /opt/interview-share-canvas; echo ${DEPLOY_PAYLOAD_BASE64} | base64 --decode | tar -x -C /opt/interview-share-canvas -f -; AWS_REGION=${AWS_REGION} IMAGE_URI=${REPOSITORY_URI}:${IMAGE_TAG} PUBLIC_BASE_URL=${APPLICATION_URL} bash /opt/interview-share-canvas/scripts/remote-deploy.sh"
```

One tar archive carries both files, so they can never arrive out of step with
each other. `tar` unpacks `scripts/remote-deploy.sh` to
`/opt/interview-share-canvas/scripts/remote-deploy.sh` and `docker-compose.yaml`
to `/opt/interview-share-canvas/docker-compose.yaml`, which is exactly where
Step 1's `COMPOSE_FILE` expects it. `tar -C "${PROJECT_DIRECTORY}"` with relative
member paths is what makes those destinations come out right.

- [ ] **Step 3: Delete the two variables the rewrite orphaned**

`DATA_VOLUME_ID` was only ever used by the remote command that Step 2 replaced,
and `REMOTE_DEPLOY_FILE` was only used by the base64 payload it replaced. The tar
command names its members relative to `PROJECT_DIRECTORY`, so neither is needed.
Delete both lines from `scripts/deploy-aws.sh`:

```bash
REMOTE_DEPLOY_FILE="${PROJECT_DIRECTORY}/scripts/remote-deploy.sh"
DATA_VOLUME_ID="$(aws cloudformation describe-stacks --region "${AWS_REGION}" --stack-name "${STACK_NAME}" --query "Stacks[0].Outputs[?OutputKey=='DataVolumeId'].OutputValue | [0]" --output text)"
```

The `DataVolumeId` stack output stays; Tasks 8 and 9 use it to prove the two
environments have distinct volumes.

Verify nothing else referenced either one:

```bash
grep -rn "DATA_VOLUME_ID\|REMOTE_DEPLOY_FILE" scripts/
```

Expected: no output.

- [ ] **Step 4: Syntax-check both scripts**

```bash
bash -n scripts/remote-deploy.sh && bash -n scripts/deploy-aws.sh && echo "syntax ok"
```

Expected: `syntax ok`.

- [ ] **Step 5: Prove the filesystem logic exists in exactly one place**

```bash
grep -rln "mkfs.xfs" scripts/ infrastructure/
```

Expected: exactly one path, `infrastructure/cloudformation.yaml`, after Task 6
lands. Before Task 6 it is `infrastructure/cloudformation.yaml` only as well,
because Step 1 has already removed it from `scripts/remote-deploy.sh`.

```bash
grep -n "mkfs\|blkid\|fstab" scripts/remote-deploy.sh
```

Expected: no output.

- [ ] **Step 6: Prove the required-variable guards fire**

```bash
( unset AWS_REGION IMAGE_URI PUBLIC_BASE_URL; bash scripts/remote-deploy.sh; echo "exit=$?" ) 2>&1 | tail -2
```

Expected: an error naming `AWS_REGION` and a non-zero exit. It must not reach
`systemctl`, `docker`, or any AWS call.

- [ ] **Step 7: Prove the payload round-trips and fits in an SSM command**

```bash
tar -c -C . -f - scripts/remote-deploy.sh docker-compose.yaml | base64 | tr -d '\n' > /tmp/deploy-payload.b64
wc -c < /tmp/deploy-payload.b64
rm -rf /tmp/deploy-payload-check && mkdir -p /tmp/deploy-payload-check
base64 --decode < /tmp/deploy-payload.b64 | tar -x -C /tmp/deploy-payload-check -f -
find /tmp/deploy-payload-check -type f | sort
diff /tmp/deploy-payload-check/docker-compose.yaml docker-compose.yaml && echo "compose file round-tripped"
diff /tmp/deploy-payload-check/scripts/remote-deploy.sh scripts/remote-deploy.sh && echo "remote script round-tripped"
```

Expected: the byte count is a few thousand, comfortably under the SSM
`AWS-RunShellScript` parameter limit; `find` prints exactly
`/tmp/deploy-payload-check/docker-compose.yaml` and
`/tmp/deploy-payload-check/scripts/remote-deploy.sh`; both `diff` commands print
their success message and nothing else.

Clean up:

```bash
rm -rf /tmp/deploy-payload-check /tmp/deploy-payload.b64
```

- [ ] **Step 8: Commit**

```bash
git add scripts/remote-deploy.sh scripts/deploy-aws.sh
git commit -m "Run the Compose stack on the host over Systems Manager"
```

---

### Task 6: Application template for a shared registry and host preparation only

Removes registry ownership from the application template so it can be instantiated more than once, drops the bootstrap mode that only existed to order repository creation, and reduces `UserData` to host preparation.

**Files:**
- Modify: `infrastructure/cloudformation.yaml`

**Interfaces:**
- Consumes: `RepositoryName` from Task 1; the deploy path built in Task 5.
- Produces: a template instantiable as any number of environments, whose instances boot ready to run the application but do not run it. New parameters `RepositoryName` and `EnvironmentName`. Removed parameters: `DeploymentMode`. Removed output: `RepositoryUri`. Retained outputs: `ApplicationUrl`, `HealthCheckUrl`, `InstanceId`, `DataVolumeId`, all now unconditional. The `ImageTag` parameter is retained because `scripts/deploy-aws.sh` still passes it, but it no longer appears anywhere in `UserData`, so a deploy can never trigger instance replacement.

- [ ] **Step 1: Replace the parameter block**

Delete the `DeploymentMode` parameter (currently `infrastructure/cloudformation.yaml:5-11`) and add the two new ones. The parameter section becomes:

```yaml
Parameters:
  EnvironmentName:
    Type: String
    AllowedValues:
      - dev
      - prod
    Description: Environment this stack instance represents.
  RepositoryName:
    Type: String
    Default: interview-share-canvas-app
    Description: Shared ECR repository owned by the registry stack.
  ImageTag:
    Type: String
    Default: latest
    AllowedPattern: "^[A-Za-z0-9_.-]{1,128}$"
  VpcId:
    Type: AWS::EC2::VPC::Id
  SubnetId:
    Type: AWS::EC2::Subnet::Id
  AvailabilityZone:
    Type: AWS::EC2::AvailabilityZone::Name
    Description: Must be the Availability Zone containing SubnetId.
  InstanceType:
    Type: String
    Default: t3.micro
    AllowedValues:
      - t3.micro
      - t3.small
      - t3.medium
  DataVolumeSize:
    Type: Number
    Default: 10
    MinValue: 8
    MaxValue: 1024
  AllowedHttpCidr:
    Type: String
    Default: 0.0.0.0/0
    AllowedPattern: "^(?:[0-9]{1,3}\\.){3}[0-9]{1,3}/(?:[0-9]|[12][0-9]|3[0-2])$"
  LatestAmiId:
    Type: AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>
    Default: /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64
```

- [ ] **Step 2: Delete the Conditions block**

Delete the whole `Conditions` block. Its only member, `IsDeploy`, exists solely
to support `DeploymentMode`, which this task removes. Add nothing in its place:
the repository URI and ARN are derived inline where they are used, in Steps 4
and 5, which keeps the two values from drifting apart.

```bash
grep -n "^Conditions:" infrastructure/cloudformation.yaml
```

Expected after the edit: no output.

- [ ] **Step 3: Delete the ContainerRepository resource**

Delete the whole `ContainerRepository` resource (currently `infrastructure/cloudformation.yaml:47-71`). Its `DeletionPolicy: Retain` means removing it from an existing stack orphans the repository rather than deleting it, but no existing stack is updated with this template anyway.

- [ ] **Step 4: Point the instance role at the shared repository**

In `ApplicationRole`, replace `Resource: !GetAtt ContainerRepository.Arn` with:

```yaml
                Resource: !Sub arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/${RepositoryName}
```

- [ ] **Step 5: Reduce `UserData` to host preparation**

`UserData` currently logs in to ECR, pulls the image, reads the public IPv4
address from the instance metadata service, and runs the container. All of that
moves to `scripts/remote-deploy.sh`, which Task 5 already rewrote. What is left
is: install Docker, install the Compose plugin, mount the EBS volume, create the
two directories the deploy needs, and stop.

**Verified constraint, checked on the live host on 2026-08-30.** Amazon Linux
2023 does not package the Compose plugin: `docker compose` is not a docker
command, `dnf list --available docker-compose-plugin` returns `No matching
Packages`, and `/usr/libexec/docker/cli-plugins/` contains only `docker-buildx`.
The plugin must therefore be downloaded. Pinned version: **v5.5.0**. The binary
is verified against the release's published
`docker-compose-linux-x86_64.sha256` **before** it is made executable and before
any use. An unverified binary is never executed.

Replace the entire `UserData` property of `ApplicationInstance` with:

```yaml
      UserData:
        Fn::Base64: !Sub |
          #!/bin/bash
          set -euxo pipefail

          dnf install -y docker xfsprogs
          systemctl enable --now docker

          COMPOSE_VERSION=v5.5.0
          COMPOSE_PLUGIN_DIRECTORY=/usr/libexec/docker/cli-plugins
          COMPOSE_ASSET=docker-compose-linux-x86_64
          COMPOSE_RELEASE_URL=https://github.com/docker/compose/releases/download/$COMPOSE_VERSION
          mkdir -p "$COMPOSE_PLUGIN_DIRECTORY"
          cd /tmp
          curl --fail --silent --show-error --location --output "$COMPOSE_ASSET" "$COMPOSE_RELEASE_URL/$COMPOSE_ASSET"
          curl --fail --silent --show-error --location --output "$COMPOSE_ASSET.sha256" "$COMPOSE_RELEASE_URL/$COMPOSE_ASSET.sha256"
          sha256sum --check "$COMPOSE_ASSET.sha256"
          install --mode 0755 "$COMPOSE_ASSET" "$COMPOSE_PLUGIN_DIRECTORY/docker-compose"
          rm --force "$COMPOSE_ASSET" "$COMPOSE_ASSET.sha256"
          docker compose version

          DATA_VOLUME_SERIAL="${ApplicationDataVolume}"
          DATA_VOLUME_SERIAL=$(echo "$DATA_VOLUME_SERIAL" | tr -d '-')
          DATA_DEVICE=""
          for attempt in $(seq 1 60); do
            DATA_DEVICE=$(lsblk --nodeps --noheadings --output NAME,SERIAL | awk -v serial="$DATA_VOLUME_SERIAL" '$2 == serial {print "/dev/" $1}')
            if [ -n "$DATA_DEVICE" ]; then
              break
            fi
            sleep 2
          done
          test -b "$DATA_DEVICE"

          if ! blkid "$DATA_DEVICE"; then
            mkfs.xfs "$DATA_DEVICE"
          fi
          DATA_UUID=$(blkid -s UUID -o value "$DATA_DEVICE")
          mkdir -p /data
          if ! grep -q "UUID=$DATA_UUID" /etc/fstab; then
            echo "UUID=$DATA_UUID /data xfs defaults,nofail 0 2" >> /etc/fstab
          fi
          mountpoint --quiet /data || mount /data

          mkdir -p /data/postgres
          mkdir -p /opt/interview-share-canvas
```

Four notes on this block:

1. `sha256sum --check` runs before `install`, and `install --mode 0755` is the
   only thing that ever makes the file executable. If the checksum does not
   match, `set -e` aborts `UserData` and the binary is never executable, never
   run.
2. Every shell variable is written `$NAME`, never `${NAME}`. The block is inside
   `Fn::Base64: !Sub`, where `${...}` is a CloudFormation substitution. The only
   `${...}` that survives is `${ApplicationDataVolume}`, which is deliberate: it
   is the volume id, resolved by CloudFormation.
3. `chown -R 10001:10001 /data` is deliberately gone. `/data/postgres` belongs to
   the PostgreSQL container's user, and the `postgres` image entrypoint sets that
   ownership itself. Restoring the chown breaks database startup.
4. There is no ECR login, no `docker pull`, no instance-metadata lookup, and no
   `docker run`. `ImageTag` therefore no longer appears in `UserData`, so
   changing the deployed image never changes `UserData` and never replaces the
   instance.

- [ ] **Step 6: Assert `ImageTag` is gone from `UserData` and kept as a parameter**

This is the check that proves a deploy cannot replace the instance.

```bash
awk '/^      UserData:/{inside=1; next} inside && /^  [^ ]/{inside=0} inside' \
  infrastructure/cloudformation.yaml > /tmp/userdata-block.txt
wc -l < /tmp/userdata-block.txt
grep -c "ImageTag" /tmp/userdata-block.txt || true
```

Expected: the line count is non-zero, proving the extraction actually found the
block, and `grep -c` prints `0`. A `grep -c` of `0` exits 1, which is why `|| true`
is there; without it the check would look like a failure.

The parameter itself must survive, because `scripts/deploy-aws.sh` still passes it:

```bash
grep -n "^  ImageTag:" infrastructure/cloudformation.yaml
```

Expected: one match.

Confirm the application startup really is gone, and the Compose install really is present:

```bash
grep -c "docker run\|get-login-password\|169.254.169.254" /tmp/userdata-block.txt || true
grep -c "sha256sum --check" /tmp/userdata-block.txt
rm -f /tmp/userdata-block.txt
```

Expected: `0` then `1`.

- [ ] **Step 7: Remove every `Condition: IsDeploy` line**

Delete the `Condition: IsDeploy` line from all six resources that carry it: `ApplicationSecurityGroup`, `ApplicationRole`, `ApplicationInstanceProfile`, `ApplicationDataVolume`, `ApplicationInstance`, `ApplicationDataVolumeAttachment`, `ApplicationElasticIp`. Verify none remain:

```bash
grep -n "IsDeploy\|DeploymentMode\|ContainerRepository" infrastructure/cloudformation.yaml
```

Expected: no output.

- [ ] **Step 8: Tag resources with the environment**

Add an `Environment` tag alongside the existing `Name` tag on `ApplicationInstance` and on `ApplicationDataVolume`:

```yaml
        - Key: Environment
          Value: !Ref EnvironmentName
```

- [ ] **Step 9: Fix the Outputs block**

Delete the `RepositoryUri` output and every `Condition: IsDeploy` line in `Outputs`, leaving:

```yaml
Outputs:
  ApplicationUrl:
    Description: Public application URL.
    Value: !Sub http://${ApplicationElasticIp}
  HealthCheckUrl:
    Value: !Sub http://${ApplicationElasticIp}/health
  InstanceId:
    Description: Connect through AWS Systems Manager Session Manager.
    Value: !Ref ApplicationInstance
  DataVolumeId:
    Value: !Ref ApplicationDataVolume
```

- [ ] **Step 10: Validate**

```bash
aws cloudformation validate-template \
  --region us-east-1 \
  --template-body file://infrastructure/cloudformation.yaml \
  --query 'Parameters[].ParameterKey' --output json
```

Expected: exactly `EnvironmentName`, `RepositoryName`, `ImageTag`, `VpcId`, `SubnetId`, `AvailabilityZone`, `InstanceType`, `DataVolumeSize`, `AllowedHttpCidr`, `LatestAmiId`. No `DeploymentMode`.

- [ ] **Step 11: Commit**

```bash
git add infrastructure/cloudformation.yaml
git commit -m "Make the application template instantiable per environment"
```

---

### Task 7: Deploy script publish and promote modes

Teaches the deploy script to either build and publish an image, or promote an image that already exists.

**Files:**
- Modify: `scripts/deploy-aws.sh`

**Interfaces:**
- Consumes: `RepositoryName` from Task 1; stack names from the Global Constraints.
- Produces: a script driven by `STACK_NAME`, `ENVIRONMENT_NAME`, `REPOSITORY_NAME`, `IMAGE_TAG`, `PUBLISH_IMAGE`, `CLOUDFORMATION_ROLE_ARN`, `AWS_REGION`, and optionally `VPC_ID`, `SUBNET_ID`, `INSTANCE_TYPE`, `ALLOWED_HTTP_CIDR`. This task edits only the configuration block, the build-or-promote block, and the `--parameter-overrides` list. The Systems Manager payload section is the one Task 5 rewrote and must be left exactly as Task 5 left it.

- [ ] **Step 1: Replace the configuration block**

Replace the configuration block of `scripts/deploy-aws.sh`, from the `TEMPLATE_FILE=` assignment through the `VPC_ID=` assignment, with the following. `REMOTE_DEPLOY_FILE` is absent because Task 5 deleted it; do not reintroduce it.

```bash
TEMPLATE_FILE="${PROJECT_DIRECTORY}/infrastructure/cloudformation.yaml"

AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:?STACK_NAME is required, for example interview-share-canvas-dev}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:?ENVIRONMENT_NAME is required, dev or prod}"
REPOSITORY_NAME="${REPOSITORY_NAME:-interview-share-canvas-app}"
PUBLISH_IMAGE="${PUBLISH_IMAGE:-true}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
ALLOWED_HTTP_CIDR="${ALLOWED_HTTP_CIDR:-0.0.0.0/0}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "${PROJECT_DIRECTORY}" rev-parse --short=12 HEAD)}"
CLOUDFORMATION_ROLE_ARN="${CLOUDFORMATION_ROLE_ARN:-}"

CLOUDFORMATION_ROLE_ARGUMENTS=()
if [[ -n "${CLOUDFORMATION_ROLE_ARN}" ]]; then
  CLOUDFORMATION_ROLE_ARGUMENTS=(--role-arn "${CLOUDFORMATION_ROLE_ARN}")
fi

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPOSITORY_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPOSITORY_NAME}"
VPC_ID="${VPC_ID:-$(aws ec2 describe-vpcs --region "${AWS_REGION}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)}"
```

- [ ] **Step 2: Replace the bootstrap block with publish or promote**

Delete the `if ! aws cloudformation describe-stacks ... Bootstrap ... fi` block and the `REPOSITORY_URI=$(aws cloudformation describe-stacks ...)` line that follows it. In their place:

```bash
if [[ "${PUBLISH_IMAGE}" == "true" ]]; then
  echo "Building and pushing ${REPOSITORY_URI}:${IMAGE_TAG}..."
  aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  docker build --platform linux/amd64 --tag "${REPOSITORY_URI}:${IMAGE_TAG}" "${PROJECT_DIRECTORY}"
  docker push "${REPOSITORY_URI}:${IMAGE_TAG}"
else
  echo "Promoting the existing image ${REPOSITORY_URI}:${IMAGE_TAG}..."
  if ! aws ecr describe-images \
    --region "${AWS_REGION}" \
    --repository-name "${REPOSITORY_NAME}" \
    --image-ids "imageTag=${IMAGE_TAG}" >/dev/null 2>&1; then
    echo "Image tag ${IMAGE_TAG} is not present in ${REPOSITORY_NAME}. Deploy it to dev first." >&2
    exit 1
  fi
fi
```

Delete the three original build and push lines that followed the old `echo "Building and pushing ..."`, since they now live inside the `if` branch above.

- [ ] **Step 3: Pass the new parameters to CloudFormation**

In the `aws cloudformation deploy` call, replace the `--parameter-overrides` list with:

```bash
  --parameter-overrides \
    EnvironmentName="${ENVIRONMENT_NAME}" \
    RepositoryName="${REPOSITORY_NAME}" \
    ImageTag="${IMAGE_TAG}" \
    VpcId="${VPC_ID}" \
    SubnetId="${SUBNET_ID}" \
    AvailabilityZone="${AVAILABILITY_ZONE}" \
    InstanceType="${INSTANCE_TYPE}" \
    AllowedHttpCidr="${ALLOWED_HTTP_CIDR}" \
```

`DeploymentMode` is gone; do not pass it.

- [ ] **Step 4: Syntax-check the script**

```bash
bash -n scripts/deploy-aws.sh && echo "syntax ok"
```

Expected: `syntax ok`.

- [ ] **Step 5: Verify the required-variable guards actually fire**

```bash
( unset STACK_NAME ENVIRONMENT_NAME; bash scripts/deploy-aws.sh; echo "exit=$?" ) 2>&1 | tail -2
```

Expected: an error naming `STACK_NAME`, and a non-zero exit. It must not reach any AWS call.

- [ ] **Step 6: Verify the promote path rejects a missing tag**

```bash
STACK_NAME=interview-share-canvas-prod \
ENVIRONMENT_NAME=prod \
PUBLISH_IMAGE=false \
IMAGE_TAG=definitelynotarealtag \
AWS_REGION=us-east-1 \
bash scripts/deploy-aws.sh 2>&1 | tail -3
```

Expected: `Image tag definitelynotarealtag is not present in interview-share-canvas-app. Deploy it to dev first.` and a non-zero exit, with no CloudFormation call attempted.

- [ ] **Step 7: Commit**

```bash
git add scripts/deploy-aws.sh
git commit -m "Add publish and promote modes to the deploy script"
```

---

### Task 8: Provision the dev environment

Builds the new dev environment alongside the still-running original stack. This is the deploy that puts the first image into the shared repository, which is why it must come before prod. It is also the first time the Compose stack from Task 4 runs on a real host.

**Files:** none. This task runs the tooling built in Tasks 1, 4, 5, 6, and 7.

**Interfaces:**
- Consumes: everything from Tasks 1, 3, 4, 5, 6, 7.
- Produces: stack `interview-share-canvas-dev`, a healthy environment running two containers on PostgreSQL, and the first image in `interview-share-canvas-app`. Record its `InstanceId`, `DataVolumeId`, and `ApplicationUrl` for the independence check in Task 9.

- [ ] **Step 1: Confirm the original environment is still healthy first**

```bash
aws cloudformation describe-stacks --region us-east-1 --stack-name interview-share-canvas \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue | [0]" --output text
```

Curl that URL's `/health`. Expected `{"status":"ok"}`. This is the fallback if anything below goes wrong.

- [ ] **Step 2: Deploy the dev stack**

```bash
STACK_NAME=interview-share-canvas-dev \
ENVIRONMENT_NAME=dev \
REPOSITORY_NAME=interview-share-canvas-app \
PUBLISH_IMAGE=true \
AWS_REGION=us-east-1 \
IMAGE_TAG=$(git rev-parse HEAD) \
./scripts/deploy-aws.sh
```

Expected final line: `Deployment is healthy: http://<dev-ip>`.

- [ ] **Step 3: Record the dev environment's identity**

```bash
aws cloudformation describe-stacks --region us-east-1 --stack-name interview-share-canvas-dev \
  --query 'Stacks[0].Outputs' --output table
```

Record `InstanceId`, `DataVolumeId`, `ApplicationUrl`.

- [ ] **Step 4: Verify the image landed in the shared repository**

```bash
aws ecr describe-images --region us-east-1 \
  --repository-name interview-share-canvas-app \
  --query 'imageDetails[].imageTags' --output json
```

Expected: the commit SHA used above. Record the image digest:

```bash
aws ecr describe-images --region us-east-1 \
  --repository-name interview-share-canvas-app \
  --image-ids "imageTag=$(git rev-parse HEAD)" \
  --query 'imageDetails[0].imageDigest' --output text
```

- [ ] **Step 5: Confirm the host runs two containers, not one**

Every host check in this plan goes through Systems Manager. Capture the instance
id first:

```bash
DEV_INSTANCE_ID=$(aws cloudformation describe-stacks --region us-east-1 \
  --stack-name interview-share-canvas-dev \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" --output text)
echo "$DEV_INSTANCE_ID"
```

Then run the container listing. The `--parameters commands=...` shorthand avoids
JSON escaping; it splits its value on commas, so never put a comma in a command
sent this way.

```bash
command_id=$(aws ssm send-command --region us-east-1 --instance-ids "$DEV_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands='docker ps --format "{{.Names}} {{.Image}}"' \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region us-east-1 --command-id "$command_id" --instance-id "$DEV_INSTANCE_ID" || true
aws ssm get-command-invocation --region us-east-1 --command-id "$command_id" \
  --instance-id "$DEV_INSTANCE_ID" --query StandardOutputContent --output text
```

Expected: exactly two lines. One is `interview-share-canvas-app-1` running
`963656558707.dkr.ecr.us-east-1.amazonaws.com/interview-share-canvas-app:<sha>`,
the other is `interview-share-canvas-postgres-1` running `postgres:17-bookworm`.
One container, or an image tag that is not the SHA deployed in Step 2, means the
Compose deploy did not take effect; stop and read the SSM command output.

- [ ] **Step 6: Confirm the PostgreSQL data lives on the EBS volume**

```bash
command_id=$(aws ssm send-command --region us-east-1 --instance-ids "$DEV_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands='mountpoint /data; cat /data/postgres/PG_VERSION; ls /data/postgres | head -5; df -h /data/postgres | tail -1' \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region us-east-1 --command-id "$command_id" --instance-id "$DEV_INSTANCE_ID" || true
aws ssm get-command-invocation --region us-east-1 --command-id "$command_id" \
  --instance-id "$DEV_INSTANCE_ID" --query StandardOutputContent --output text
```

Expected: `/data is a mountpoint`, then `17`, then directory entries including
`base`, `global`, and `pg_wal`, then a `df` line whose device is the attached EBS
volume and not the root volume. If `/data/postgres/PG_VERSION` does not exist,
PostgreSQL initialised somewhere else and the data is not durable; stop.

- [ ] **Step 7: Confirm the application really is on PostgreSQL**

This is the check that distinguishes the new stack from the retired SQLite one.

```bash
command_id=$(aws ssm send-command --region us-east-1 --instance-ids "$DEV_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands='docker exec interview-share-canvas-app-1 python -c "from backend.main import store; print(store.engine.dialect.name)"' \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region us-east-1 --command-id "$command_id" --instance-id "$DEV_INSTANCE_ID" || true
aws ssm get-command-invocation --region us-east-1 --command-id "$command_id" \
  --instance-id "$DEV_INSTANCE_ID" --query StandardOutputContent --output text
```

Expected: `postgresql`. Anything else, and in particular `sqlite`, means
`DATABASE_URL` did not reach the container. Stop and fix before deploying prod.

- [ ] **Step 8: Confirm the original environment is still healthy**

Curl the original stack's `/health` again. Expected `{"status":"ok"}`. Building dev must not have disturbed it.

---

### Task 9: Provision the production environment

Deploys prod from the image dev just validated, without building anything.

**Files:** none.

**Interfaces:**
- Consumes: the image digest and tag recorded in Task 8.
- Produces: stack `interview-share-canvas-prod`, healthy, running the same two-container Compose stack as dev, with resources provably distinct from dev's.

- [ ] **Step 1: Deploy prod by promotion**

```bash
STACK_NAME=interview-share-canvas-prod \
ENVIRONMENT_NAME=prod \
REPOSITORY_NAME=interview-share-canvas-app \
PUBLISH_IMAGE=false \
AWS_REGION=us-east-1 \
IMAGE_TAG=$(git rev-parse HEAD) \
./scripts/deploy-aws.sh
```

Expected: `Promoting the existing image ...` then `Deployment is healthy: http://<prod-ip>`. It must not print any Docker build output.

- [ ] **Step 2: Prove the two environments share nothing but the image**

```bash
for stack in interview-share-canvas-dev interview-share-canvas-prod; do
  echo "== $stack"
  aws cloudformation describe-stacks --region us-east-1 --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'||OutputKey=='DataVolumeId'||OutputKey=='ApplicationUrl'].{K:OutputKey,V:OutputValue}" \
    --output text
done
```

Expected: all three values differ between the two stacks. Any shared value is a defect; stop and investigate.

- [ ] **Step 3: Prove prod runs the identical image dev validated**

```bash
aws ecr describe-images --region us-east-1 \
  --repository-name interview-share-canvas-app \
  --image-ids "imageTag=$(git rev-parse HEAD)" \
  --query 'imageDetails[0].imageDigest' --output text
```

Expected: identical to the digest recorded in Task 8 Step 4. Both environments reference this one tag, so both run this one digest.

- [ ] **Step 4: Health-check both environments**

Curl `/health` on both `ApplicationUrl` values. Expected `{"status":"ok"}` from each.

- [ ] **Step 5: Confirm the environment tag is visible**

```bash
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=tag:Environment,Values=prod" \
  --query 'Reservations[].Instances[].InstanceId' --output text
```

Expected: exactly the prod instance ID.

- [ ] **Step 6: Confirm the prod host runs two containers, not one**

```bash
PROD_INSTANCE_ID=$(aws cloudformation describe-stacks --region us-east-1 \
  --stack-name interview-share-canvas-prod \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" --output text)

command_id=$(aws ssm send-command --region us-east-1 --instance-ids "$PROD_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands='docker ps --format "{{.Names}} {{.Image}}"' \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region us-east-1 --command-id "$command_id" --instance-id "$PROD_INSTANCE_ID" || true
aws ssm get-command-invocation --region us-east-1 --command-id "$command_id" \
  --instance-id "$PROD_INSTANCE_ID" --query StandardOutputContent --output text
```

Expected: exactly two lines, `interview-share-canvas-app-1` on the same ECR image
and tag dev is running, and `interview-share-canvas-postgres-1` on
`postgres:17-bookworm`.

- [ ] **Step 7: Confirm the prod PostgreSQL data lives on its own EBS volume**

```bash
command_id=$(aws ssm send-command --region us-east-1 --instance-ids "$PROD_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands='mountpoint /data; cat /data/postgres/PG_VERSION; ls /data/postgres | head -5; df -h /data/postgres | tail -1' \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region us-east-1 --command-id "$command_id" --instance-id "$PROD_INSTANCE_ID" || true
aws ssm get-command-invocation --region us-east-1 --command-id "$command_id" \
  --instance-id "$PROD_INSTANCE_ID" --query StandardOutputContent --output text
```

Expected: `/data is a mountpoint`, then `17`, then `base`, `global`, `pg_wal`
among the entries, then a `df` line for the attached volume. The volume id in
Step 2's output already proved this is not dev's volume.

- [ ] **Step 8: Confirm prod is on PostgreSQL**

```bash
command_id=$(aws ssm send-command --region us-east-1 --instance-ids "$PROD_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters commands='docker exec interview-share-canvas-app-1 python -c "from backend.main import store; print(store.engine.dialect.name)"' \
  --query Command.CommandId --output text)
aws ssm wait command-executed --region us-east-1 --command-id "$command_id" --instance-id "$PROD_INSTANCE_ID" || true
aws ssm get-command-invocation --region us-east-1 --command-id "$command_id" \
  --instance-id "$PROD_INSTANCE_ID" --query StandardOutputContent --output text
```

Expected: `postgresql`.

- [ ] **Step 9: Prove the two databases are genuinely separate**

Create a row in dev and confirm prod cannot see it. `/v1/auth/magic-link` returns
a session token in the `x-session-token` header, which is enough to create a
session.

```bash
DEV_URL=$(aws cloudformation describe-stacks --region us-east-1 --stack-name interview-share-canvas-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue | [0]" --output text)
PROD_URL=$(aws cloudformation describe-stacks --region us-east-1 --stack-name interview-share-canvas-prod \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue | [0]" --output text)

dev_token=$(curl --fail --silent --show-error --dump-header - --output /dev/null \
  --header 'Content-Type: application/json' \
  --data '{"email":"isolation-check@example.com"}' \
  "$DEV_URL/v1/auth/magic-link" | awk 'tolower($1)=="x-session-token:"{print $2}' | tr -d '\r')

curl --fail --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  --header "Authorization: Bearer $dev_token" \
  --header 'Content-Type: application/json' \
  --data '{"title":"isolation check","prompt":"Confirm dev and prod use separate databases.","duration_minutes":30,"template_id":"blank"}' \
  "$DEV_URL/v1/sessions"

curl --silent --output /dev/null --write-out '%{http_code}\n' \
  --header "Authorization: Bearer $dev_token" \
  "$PROD_URL/v1/sessions"
```

Expected: `201` from the dev creation, and `401` from prod, because the dev
session token does not exist in prod's database. A `200` from prod would mean
the two environments share a database; stop immediately.

---

### Task 10: Two-stage deployment pipeline

Replaces the single deploy job with an automatic dev deploy and an approval-gated prod promotion.

**Files:**
- Modify: `.github/workflows/ci-cd.yaml`

**Interfaces:**
- Consumes: the GitHub environments and variables from Tasks 2 and 3; the script contract from Task 7.
- Produces: jobs `deploy-dev` and `deploy-prod`.

- [ ] **Step 1: Replace the `deploy` job**

Delete the entire existing `deploy:` job and put these two in its place. Keep the action SHA pins exactly as they appear elsewhere in the file.

```yaml
  deploy-dev:
    name: Deploy to dev
    if: github.event_name != 'pull_request'
    needs: compose-tests
    runs-on: ubuntu-latest
    environment: development
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Configure AWS credentials through GitHub OIDC
        uses: aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c # v6.2.3
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
      - name: Build, publish, and deploy
        env:
          AWS_REGION: ${{ vars.AWS_REGION }}
          STACK_NAME: ${{ vars.AWS_STACK_NAME }}
          ENVIRONMENT_NAME: dev
          REPOSITORY_NAME: ${{ vars.AWS_REPOSITORY_NAME }}
          CLOUDFORMATION_ROLE_ARN: ${{ vars.AWS_CLOUDFORMATION_ROLE_ARN }}
          PUBLISH_IMAGE: "true"
          IMAGE_TAG: ${{ github.sha }}
        run: ./scripts/deploy-aws.sh
      - name: Validate deployment health
        env:
          AWS_REGION: ${{ vars.AWS_REGION }}
          STACK_NAME: ${{ vars.AWS_STACK_NAME }}
        run: |
          application_url=$(aws cloudformation describe-stacks \
            --region "$AWS_REGION" \
            --stack-name "$STACK_NAME" \
            --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue | [0]" \
            --output text)
          curl --fail --show-error --silent --retry 12 --retry-all-errors \
            --retry-delay 5 "$application_url/health" | grep '"status":"ok"'

  deploy-prod:
    name: Deploy to production
    needs: deploy-dev
    runs-on: ubuntu-latest
    environment: production
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Configure AWS credentials through GitHub OIDC
        uses: aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c # v6.2.3
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
      - name: Promote the tested image
        env:
          AWS_REGION: ${{ vars.AWS_REGION }}
          STACK_NAME: ${{ vars.AWS_STACK_NAME }}
          ENVIRONMENT_NAME: prod
          REPOSITORY_NAME: ${{ vars.AWS_REPOSITORY_NAME }}
          CLOUDFORMATION_ROLE_ARN: ${{ vars.AWS_CLOUDFORMATION_ROLE_ARN }}
          PUBLISH_IMAGE: "false"
          IMAGE_TAG: ${{ github.sha }}
        run: ./scripts/deploy-aws.sh
      - name: Validate deployment health
        env:
          AWS_REGION: ${{ vars.AWS_REGION }}
          STACK_NAME: ${{ vars.AWS_STACK_NAME }}
        run: |
          application_url=$(aws cloudformation describe-stacks \
            --region "$AWS_REGION" \
            --stack-name "$STACK_NAME" \
            --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue | [0]" \
            --output text)
          curl --fail --show-error --silent --retry 12 --retry-all-errors \
            --retry-delay 5 "$application_url/health" | grep '"status":"ok"'
```

`deploy-prod` needs no `if:` of its own. It depends on `deploy-dev`, which is skipped on pull requests, so it is skipped too.

- [ ] **Step 2: Check the workflow parses**

```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/ci-cd.yaml')); print(sorted(d['jobs']))"
```

Expected: `['backend-tests', 'compose-tests', 'deploy-dev', 'deploy-prod', 'frontend-tests']`.

- [ ] **Step 3: Confirm no stack name is hardcoded any more**

```bash
grep -n "interview-share-canvas" .github/workflows/ci-cd.yaml
```

Expected: only the Compose project name for the test job. No `--stack-name interview-share-canvas`.

- [ ] **Step 4: Run the full local test suite before pushing**

```bash
uv run pytest
npm --prefix frontend ci && npm --prefix frontend test
```

Expected: all green.

- [ ] **Step 5: Commit and push**

```bash
git add .github/workflows/ci-cd.yaml
git commit -m "Deploy to dev automatically and promote to production on approval"
git push origin main
```

- [ ] **Step 6: Watch the run and confirm it stops at the gate**

```bash
run_id=$(gh run list --workflow ci-cd.yaml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view "$run_id" --json jobs --jq '.jobs[] | "\(.conclusion // .status)\t\(.name)"'
```

Expected: tests and `Deploy to dev` succeed, and `Deploy to production` sits `waiting`. **The run pausing here is the central acceptance criterion of this whole plan.** Confirm it does not proceed on its own.

- [ ] **Step 7: Approve the promotion and confirm it completes**

Approve in the Actions UI, then:

```bash
until [ "$(gh run view "$run_id" --json status --jq .status)" = "completed" ]; do sleep 20; done
gh run view "$run_id" --json conclusion,jobs --jq '"\(.conclusion)", (.jobs[] | "  \(.conclusion)\t\(.name)")'
```

Expected: `success` for all five jobs.

- [ ] **Step 8: Confirm both environments serve the new commit**

Curl `/health` on both environments. Then confirm the running containers on each host over SSM:

```bash
for stack in interview-share-canvas-dev interview-share-canvas-prod; do
  iid=$(aws cloudformation describe-stacks --region us-east-1 --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" --output text)
  cid=$(aws ssm send-command --region us-east-1 --instance-ids "$iid" \
    --document-name AWS-RunShellScript \
    --parameters commands='docker ps --format "{{.Names}} {{.Image}}"' \
    --query 'Command.CommandId' --output text)
  aws ssm wait command-executed --region us-east-1 --command-id "$cid" --instance-id "$iid" || true
  echo "== $stack"
  aws ssm get-command-invocation --region us-east-1 --command-id "$cid" --instance-id "$iid" \
    --query StandardOutputContent --output text
done
```

Expected: each host prints two lines. `interview-share-canvas-app-1` on the same
ECR repository and the same commit SHA tag in both environments, and
`interview-share-canvas-postgres-1` on `postgres:17-bookworm` in both. A single
line from either host means the Compose deploy did not run there.

Then confirm both are still on PostgreSQL after the pipeline deploy:

```bash
for stack in interview-share-canvas-dev interview-share-canvas-prod; do
  iid=$(aws cloudformation describe-stacks --region us-east-1 --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" --output text)
  cid=$(aws ssm send-command --region us-east-1 --instance-ids "$iid" \
    --document-name AWS-RunShellScript \
    --parameters commands='docker exec interview-share-canvas-app-1 python -c "from backend.main import store; print(store.engine.dialect.name)"' \
    --query 'Command.CommandId' --output text)
  aws ssm wait command-executed --region us-east-1 --command-id "$cid" --instance-id "$iid" || true
  echo "$stack: $(aws ssm get-command-invocation --region us-east-1 --command-id "$cid" --instance-id "$iid" --query StandardOutputContent --output text)"
done
```

Expected: `postgresql` from both.

---

### Task 11: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the finished system.
- Produces: documentation matching what was actually built.

- [ ] **Step 1: Update the layout table**

Add `infrastructure/registry.yaml` to the Repository layout table's `infrastructure/` row description, noting it owns the shared registry.

- [ ] **Step 2: Rewrite the CI/CD pipeline diagram**

Replace the existing diagram with:

```
Backend tests  --+
                 +--> Compose integration --> Deploy to dev --> Deploy to production
Frontend tests --+       and E2E tests         (automatic)       (waits for approval)
```

Then update the numbered list beneath it so that the deploy entry becomes two
entries: dev builds and publishes the image tagged with the commit SHA and
deploys it; production deploys that same tag without building. State plainly
that the production role holds no ECR write permission, so a promotion cannot
publish an image even if the pipeline asked it to, and that the approval gate
also gates AWS access because the OIDC token is minted only once the job starts.

- [ ] **Step 3: Document the two environments**

Add a short table naming each environment, its stack, and how it is reached. Fill in the two `ApplicationUrl` values recorded in Tasks 8 and 9.

- [ ] **Step 4: Update the AWS deploy section**

The variables table for `scripts/deploy-aws.sh` must gain `STACK_NAME` (now required), `ENVIRONMENT_NAME` (required), `REPOSITORY_NAME`, and `PUBLISH_IMAGE`, and lose the description of `DeploymentMode` bootstrapping.

- [ ] **Step 5: Document what actually runs on a host**

Any statement that a host runs a single container, or that it stores data in
SQLite on the EBS volume, is now false and must be replaced. State instead:

- Each host runs the repository's own `docker-compose.yaml`: the application
  container plus `postgres:17-bookworm`. It is the same file, and therefore the
  same stack, that the `compose-tests` CI job validates.
- The PostgreSQL data directory is `/data/postgres` on the encrypted EBS volume.
  The volume is formatted and mounted by the instance `UserData`; nothing else
  formats it.
- The host overrides are `APP_IMAGE` (the ECR image and tag), `APP_PORT=80`,
  `POSTGRES_DATA=/data/postgres`, `RESTART_POLICY=unless-stopped`, and
  `PUBLIC_BASE_URL`. Unset, every one of them falls back to the local
  development default, which is why one compose file serves all three
  environments.
- `UserData` prepares the host and stops: Docker, the pinned Compose plugin, and
  the mounted volume. `scripts/remote-deploy.sh`, run over Systems Manager, is
  the only thing that starts the application. Because `ImageTag` no longer
  appears in `UserData`, deploying a new image never replaces the instance.
- Amazon Linux 2023 does not package the Compose plugin, so `UserData`
  downloads Docker Compose v5.5.0 and verifies it against the release's
  published SHA-256 checksum before making it executable.

- [ ] **Step 6: Document how to inspect a running host**

Document these two commands, with the outputs recorded in Tasks 8 and 9:

```bash
docker ps --format '{{.Names}} {{.Image}}'
docker exec interview-share-canvas-app-1 python -c "from backend.main import store; print(store.engine.dialect.name)"
```

Expected, and state it in the README: two containers, and `postgresql`.

- [ ] **Step 7: Update the bootstrap instructions**

The OIDC bootstrap section must show the registry stack first, then the two per-environment OIDC stacks with `ApplicationStackName`, `OidcSubject`, and `AllowImagePublish`. State that `OidcSubject` is discovered, never constructed, and say why.

- [ ] **Step 8: Record the TLS follow-up**

Add a short "Known gaps" note stating that both environments serve plain HTTP and that TLS and a domain are required before production carries real interview content.

- [ ] **Step 9: Verify every documented command**

Run each command block the README now contains that is safe to run, and confirm the output matches what is documented. Do not document a command you have not run.

- [ ] **Step 10: Commit and push**

```bash
git add README.md
git commit -m "Document the dev and production environments"
git push origin main
```

Note: this push triggers a pipeline run that will pause at the production gate. Approve or leave it pending as you prefer.

---

### Task 12: Retire the original stack

**Destructive. Do not begin without explicit confirmation from the user at the time.** The user's approval of the design and this plan does not authorize these deletions.

**Files:** none.

**Interfaces:**
- Consumes: verified dev and prod environments from Tasks 8, 9, and 10.
- Produces: the old stack, its orphaned repository, and the old OIDC stack removed.

- [ ] **Step 1: Re-confirm the replacements are healthy**

Health-check both new environments. Both must return `{"status":"ok"}`. If either is unhealthy, stop.

- [ ] **Step 2: Ask the user to confirm the deletion explicitly**

State what will be deleted, what is recoverable, and what is not:
- Stack `interview-share-canvas`: its EC2 instance and Elastic IP are destroyed. Its data volume is **snapshotted**, not deleted, because of `DeletionPolicy: Snapshot`.
- Repository `interview-share-canvas`: its images are destroyed and are not recoverable, though they are rebuildable from git.
- Stack `interview-share-canvas-github-oidc`: its two roles are destroyed.

Wait for a clear yes. Do not infer approval.

- [ ] **Step 3: Delete the original application stack**

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name interview-share-canvas
aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name interview-share-canvas
```

- [ ] **Step 4: Confirm the data snapshot exists**

```bash
aws ec2 describe-snapshots --region us-east-1 --owner-ids self \
  --query 'sort_by(Snapshots,&StartTime)[-3:].{Id:SnapshotId,Time:StartTime,Desc:Description}' \
  --output table
```

Expected: a recent snapshot of the retired volume. If none exists, stop and investigate before deleting anything else.

- [ ] **Step 5: Delete the orphaned repository**

```bash
aws ecr delete-repository --region us-east-1 \
  --repository-name interview-share-canvas --force
```

- [ ] **Step 6: Delete the superseded OIDC stack**

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name interview-share-canvas-github-oidc
aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name interview-share-canvas-github-oidc
```

- [ ] **Step 7: Final inventory**

```bash
aws cloudformation list-stacks --region us-east-1 \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?starts_with(StackName,`interview-share-canvas`)].StackName' --output json
aws ecr describe-repositories --region us-east-1 \
  --query 'repositories[].repositoryName' --output json
```

Expected stacks: `interview-share-canvas-registry`, `interview-share-canvas-dev`, `interview-share-canvas-prod`, `interview-share-canvas-dev-oidc`, `interview-share-canvas-prod-oidc`. Expected repository: `interview-share-canvas-app` only.

- [ ] **Step 8: Run the pipeline once more**

Push any trivial change, or re-run the latest workflow, and confirm a full green run through both environments. This proves nothing depended on the deleted resources.

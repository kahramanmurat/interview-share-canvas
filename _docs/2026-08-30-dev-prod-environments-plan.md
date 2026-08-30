# Dev and Production Environments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single AWS deployment into an automatically deployed `dev` environment and a manually approved `prod` environment that runs the exact image `dev` validated.

**Architecture:** One shared ECR repository owned by its own CloudFormation stack, plus two identical application stacks and two independent pairs of IAM roles. The pipeline builds and pushes once during the dev deploy, then promotes that same image tag to prod behind a GitHub Environment approval gate.

**Tech Stack:** AWS CloudFormation, ECR, EC2, EBS, Systems Manager, IAM with GitHub OIDC federation, GitHub Actions, Bash, Docker.

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

- [ ] **Step 1: Create the two GitHub environments**

```bash
gh api -X PUT repos/kahramanmurat/interview-share-canvas/environments/development
gh api -X PUT repos/kahramanmurat/interview-share-canvas/environments/production \
  -F "reviewers[][type]=User" \
  -F "reviewers[][id]=$(gh api user --jq .id)" \
  -F "deployment_branch_policy[protected_branches]=true" \
  -F "deployment_branch_policy[custom_branch_policies]=false"
```

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
DEV_SUBJECT  = ____________________________________________
PROD_SUBJECT = ____________________________________________
```

Expected shape, to sanity-check rather than to assume: something ending in `:environment:development` and `:environment:production`. If the prefix is **not** the immutable `repo:kahramanmurat@1132768/interview-share-canvas@1350826572` form, that is important information, not a problem. Use exactly what was printed.

- [ ] **Step 6: Delete the probe workflow**

```bash
git rm .github/workflows/oidc-subject-probe.yaml
git commit -m "Remove temporary OIDC subject probe"
```

Do not push yet. Task 3 pushes nothing either; the next push happens in Task 8.

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

### Task 4: Application template for a shared registry

Removes registry ownership from the application template so it can be instantiated more than once, and drops the bootstrap mode that only existed to order repository creation.

**Files:**
- Modify: `infrastructure/cloudformation.yaml`

**Interfaces:**
- Consumes: `RepositoryName` from Task 1.
- Produces: a template instantiable as any number of environments. New parameters `RepositoryName` and `EnvironmentName`. Removed parameters: `DeploymentMode`. Removed output: `RepositoryUri`. Retained outputs: `ApplicationUrl`, `HealthCheckUrl`, `InstanceId`, `DataVolumeId`, all now unconditional.

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

- [ ] **Step 5: Point UserData at the shared repository**

In `ApplicationInstance` `UserData`, replace the line

```
          IMAGE_URI="${ContainerRepository.RepositoryUri}:${ImageTag}"
```

with

```
          IMAGE_URI="${AWS::AccountId}.dkr.ecr.${AWS::Region}.${AWS::URLSuffix}/${RepositoryName}:${ImageTag}"
```

- [ ] **Step 6: Remove every `Condition: IsDeploy` line**

Delete the `Condition: IsDeploy` line from all six resources that carry it: `ApplicationSecurityGroup`, `ApplicationRole`, `ApplicationInstanceProfile`, `ApplicationDataVolume`, `ApplicationInstance`, `ApplicationDataVolumeAttachment`, `ApplicationElasticIp`. Verify none remain:

```bash
grep -n "IsDeploy\|DeploymentMode\|ContainerRepository" infrastructure/cloudformation.yaml
```

Expected: no output.

- [ ] **Step 7: Tag resources with the environment**

Add an `Environment` tag alongside the existing `Name` tag on `ApplicationInstance` and on `ApplicationDataVolume`:

```yaml
        - Key: Environment
          Value: !Ref EnvironmentName
```

- [ ] **Step 8: Fix the Outputs block**

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

- [ ] **Step 9: Validate**

```bash
aws cloudformation validate-template \
  --region us-east-1 \
  --template-body file://infrastructure/cloudformation.yaml \
  --query 'Parameters[].ParameterKey' --output json
```

Expected: exactly `EnvironmentName`, `RepositoryName`, `ImageTag`, `VpcId`, `SubnetId`, `AvailabilityZone`, `InstanceType`, `DataVolumeSize`, `AllowedHttpCidr`, `LatestAmiId`. No `DeploymentMode`.

- [ ] **Step 10: Commit**

```bash
git add infrastructure/cloudformation.yaml
git commit -m "Make the application template instantiable per environment"
```

---

### Task 5: Deploy script publish and promote modes

Teaches the deploy script to either build and publish an image, or promote an image that already exists.

**Files:**
- Modify: `scripts/deploy-aws.sh`

**Interfaces:**
- Consumes: `RepositoryName` from Task 1; stack names from the Global Constraints.
- Produces: a script driven by `STACK_NAME`, `ENVIRONMENT_NAME`, `REPOSITORY_NAME`, `IMAGE_TAG`, `PUBLISH_IMAGE`, `CLOUDFORMATION_ROLE_ARN`, `AWS_REGION`, and optionally `VPC_ID`, `SUBNET_ID`, `INSTANCE_TYPE`, `ALLOWED_HTTP_CIDR`. `scripts/remote-deploy.sh` is unchanged.

- [ ] **Step 1: Replace the configuration block**

Replace lines 6 through 24 of `scripts/deploy-aws.sh` (from `TEMPLATE_FILE=` through the `AWS_ACCOUNT_ID`/`VPC_ID` assignments) with:

```bash
TEMPLATE_FILE="${PROJECT_DIRECTORY}/infrastructure/cloudformation.yaml"
REMOTE_DEPLOY_FILE="${PROJECT_DIRECTORY}/scripts/remote-deploy.sh"

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

### Task 6: Provision the dev environment

Builds the new dev environment alongside the still-running original stack. This is the deploy that puts the first image into the shared repository, which is why it must come before prod.

**Files:** none. This task runs the tooling built in Tasks 1, 4, and 5.

**Interfaces:**
- Consumes: everything from Tasks 1, 3, 4, 5.
- Produces: stack `interview-share-canvas-dev`, a healthy environment, and the first image in `interview-share-canvas-app`. Record its `InstanceId`, `DataVolumeId`, and `ApplicationUrl` for the independence check in Task 7.

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

- [ ] **Step 5: Confirm the original environment is still healthy**

Curl the original stack's `/health` again. Expected `{"status":"ok"}`. Building dev must not have disturbed it.

---

### Task 7: Provision the production environment

Deploys prod from the image dev just validated, without building anything.

**Files:** none.

**Interfaces:**
- Consumes: the image digest and tag recorded in Task 6.
- Produces: stack `interview-share-canvas-prod`, healthy, with resources provably distinct from dev's.

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

Expected: identical to the digest recorded in Task 6 Step 4. Both environments reference this one tag, so both run this one digest.

- [ ] **Step 4: Health-check both environments**

Curl `/health` on both `ApplicationUrl` values. Expected `{"status":"ok"}` from each.

- [ ] **Step 5: Confirm the environment tag is visible**

```bash
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=tag:Environment,Values=prod" \
  --query 'Reservations[].Instances[].InstanceId' --output text
```

Expected: exactly the prod instance ID.

---

### Task 8: Two-stage deployment pipeline

Replaces the single deploy job with an automatic dev deploy and an approval-gated prod promotion.

**Files:**
- Modify: `.github/workflows/ci-cd.yaml`

**Interfaces:**
- Consumes: the GitHub environments and variables from Tasks 2 and 3; the script contract from Task 5.
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

Curl `/health` on both environments. Then confirm the running image on each host matches `${{ github.sha }}` using SSM, as done previously:

```bash
for stack in interview-share-canvas-dev interview-share-canvas-prod; do
  iid=$(aws cloudformation describe-stacks --region us-east-1 --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" --output text)
  cid=$(aws ssm send-command --region us-east-1 --instance-ids "$iid" \
    --document-name AWS-RunShellScript \
    --parameters '{"commands":["docker ps --format \"{{.Image}}\""]}' \
    --query 'Command.CommandId' --output text)
  aws ssm wait command-executed --region us-east-1 --command-id "$cid" --instance-id "$iid" || true
  echo "$stack: $(aws ssm get-command-invocation --region us-east-1 --command-id "$cid" --instance-id "$iid" --query StandardOutputContent --output text)"
done
```

Expected: both print the same repository and the same commit SHA tag.

---

### Task 9: Documentation

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
Backend tests  ─┐
                ├─> Compose integration ─> Deploy to dev ─> Deploy to production
Frontend tests ─┘      and E2E tests        (automatic)      (waits for approval)
```

Then update the numbered list beneath it so that the deploy entry becomes two
entries: dev builds and publishes the image tagged with the commit SHA and
deploys it; production deploys that same tag without building. State plainly
that the production role holds no ECR write permission, so a promotion cannot
publish an image even if the pipeline asked it to, and that the approval gate
also gates AWS access because the OIDC token is minted only once the job starts.

- [ ] **Step 3: Document the two environments**

Add a short table naming each environment, its stack, and how it is reached. Fill in the two `ApplicationUrl` values recorded in Tasks 6 and 7.

- [ ] **Step 4: Update the AWS deploy section**

The variables table for `scripts/deploy-aws.sh` must gain `STACK_NAME` (now required), `ENVIRONMENT_NAME` (required), `REPOSITORY_NAME`, and `PUBLISH_IMAGE`, and lose the description of `DeploymentMode` bootstrapping.

- [ ] **Step 5: Update the bootstrap instructions**

The OIDC bootstrap section must show the registry stack first, then the two per-environment OIDC stacks with `ApplicationStackName`, `OidcSubject`, and `AllowImagePublish`. State that `OidcSubject` is discovered, never constructed, and say why.

- [ ] **Step 6: Record the TLS follow-up**

Add a short "Known gaps" note stating that both environments serve plain HTTP and that TLS and a domain are required before production carries real interview content.

- [ ] **Step 7: Verify every documented command**

Run each command block the README now contains that is safe to run, and confirm the output matches what is documented. Do not document a command you have not run.

- [ ] **Step 8: Commit and push**

```bash
git add README.md
git commit -m "Document the dev and production environments"
git push origin main
```

Note: this push triggers a pipeline run that will pause at the production gate. Approve or leave it pending as you prefer.

---

### Task 10: Retire the original stack

**Destructive. Do not begin without explicit confirmation from the user at the time.** The user's approval of the design and this plan does not authorize these deletions.

**Files:** none.

**Interfaces:**
- Consumes: verified dev and prod environments from Tasks 6, 7, and 8.
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

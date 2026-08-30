# Dev and production environments

Date: 2026-08-30
Status: Approved, not yet implemented

## Goal

Run two independent copies of the application: the existing deployment becomes
`dev`, and a new, separately provisioned copy becomes `prod`. A change reaches
production only after it has passed every test and been deployed to dev, and
only after a human approves the promotion.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Promotion trigger | Manual approval gate on a GitHub Environment | One pipeline and one artifact per commit, with an explicit human gate. Prod cannot drift behind main the way tag- or branch-based promotion allows. |
| Isolation | Separate stacks in AWS account `963656558707` | The account is not part of an AWS Organization, so account-level isolation would need an account created out of band. Separate stacks still give each environment its own compute, storage, address, and IAM roles. |
| Artifact | Build once, promote the image | Prod runs the identical image that passed dev. Requires moving the ECR repository out of the per-environment stack. |
| Dev stack name | Recreate as `interview-share-canvas-dev` | CloudFormation cannot rename a stack. Symmetric naming is worth a dev IP change and a dev database reset; `ApplicationDataVolume` uses `DeletionPolicy: Snapshot`, so the old data is recoverable. |
| TLS | Out of scope, tracked as a follow-up | The stack serves plain HTTP. Naming a copy "production" does not change that. Recorded below rather than silently expanded into this change. |

## Target architecture

```
interview-share-canvas-registry        shared ECR repository
        |
        +-- interview-share-canvas-dev      EC2 + EBS + Elastic IP + security group
        +-- interview-share-canvas-prod     EC2 + EBS + Elastic IP + security group

interview-share-canvas-dev-oidc        deploy role + CloudFormation execution role
interview-share-canvas-prod-oidc       deploy role + CloudFormation execution role
```

Only the image registry is shared. Each environment has its own instance, its
own encrypted EBS data volume, its own public address, and its own pair of IAM
roles. Neither environment's roles can act on the other's stack.

## Component changes

### New: `infrastructure/registry.yaml`

Owns a single ECR repository named `interview-share-canvas-app`, carrying over
the current settings: `ScanOnPush`, `ImageTagMutability: IMMUTABLE`, the
keep-20-newest lifecycle policy, and `DeletionPolicy: Retain`.

The name differs from the existing repository (`interview-share-canvas`) on
purpose. The existing repository is owned by the stack being retired, and two
repositories cannot share a name, so a new name lets the new environments be
built and verified before anything is deleted.

Outputs: `RepositoryUri`, `RepositoryArn`, `RepositoryName`.

### Changed: `infrastructure/cloudformation.yaml`

- Remove the `ContainerRepository` resource. Because it is `DeletionPolicy:
  Retain`, removing it from a live stack orphans the repository rather than
  deleting it.
- Add `RepositoryUri` and `RepositoryArn` parameters, supplied by the registry
  stack's outputs.
- Replace `!GetAtt ContainerRepository.RepositoryUri` in `UserData` and
  `!GetAtt ContainerRepository.Arn` in the instance role policy with those
  parameters.
- Remove the `DeploymentMode` parameter and the `IsDeploy` condition. Their only
  purpose was to create the repository before the first image existed; a
  separate registry stack removes that ordering problem, so every resource is
  created unconditionally.
- Remove the `RepositoryUri` output, which now belongs to the registry stack.
- Add an `EnvironmentName` parameter, tagged onto the instance and volume so
  environment is visible in the console and in cost reports.

### Changed: `infrastructure/github-oidc.yaml`

Deployed once per environment instead of once per repository.

- Add `EnvironmentName` (`dev` or `prod`). Role names become
  `interview-share-canvas-<env>-github-deploy` and
  `interview-share-canvas-<env>-cloudformation`.
- Add `RepositoryArn`, so the registry can be referenced without the app stack
  owning it.
- Trust condition changes from a branch subject to an environment subject, so
  that GitHub Environment protection rules gate the token itself. The exact
  subject string is verified empirically before this is applied; see
  "Unverified detail" below.
- The dev deploy role keeps ECR push permissions. **The prod deploy role gets
  only `ecr:DescribeImages`**, so the prod path can confirm the tag exists but
  cannot publish an image.
- Each role's CloudFormation and SSM permissions are scoped to its own stack, as
  today: stack-ARN-scoped CloudFormation actions, and `ssm:SendCommand`
  restricted to the `AWS-RunShellScript` document plus instances tagged
  `aws:cloudformation:stack-name = <that environment's stack>`.
- The CloudFormation execution role no longer needs `ecr:*`, since the app stack
  no longer creates a repository.

### Changed: `scripts/deploy-aws.sh`

- Add `PUBLISH_IMAGE` (default `true`). When `false`, skip the Docker build,
  the ECR login, and the push, and deploy the stack with the supplied
  `IMAGE_TAG`. The prod path sets `false`.
- Add `REPOSITORY_URI`, required, replacing the value previously read from the
  app stack's outputs.
- When `PUBLISH_IMAGE=false`, verify the tag exists in ECR with
  `aws ecr describe-images` and fail early with a clear message if it does not.

The flag is a convenience, not the security control. The control is IAM: the
prod role has no ECR write permission, so a prod deploy cannot publish an image
even if the flag were wrong.

`scripts/remote-deploy.sh` needs no changes. It already takes every input as an
environment variable and derives the registry host from `IMAGE_URI`.

### Changed: `.github/workflows/ci-cd.yaml`

```
backend-tests  ─┐
                ├─> compose-tests ─> deploy-dev ─> deploy-prod
frontend-tests ─┘                    (automatic)   (approval gate)
```

- `deploy-dev`: `environment: development`. Builds, pushes tag `<sha>`, deploys
  the dev stack, health-checks.
- `deploy-prod`: `needs: deploy-dev`, `environment: production`. Deploys the
  same `<sha>` with `PUBLISH_IMAGE=false`, health-checks. Because the OIDC token
  is minted only when the job starts, the environment's required reviewers gate
  AWS access as well as the deployment.
- The health-check step's hardcoded `--stack-name interview-share-canvas`
  becomes the per-job stack name.
- Pull requests continue to run tests only.

### GitHub repository configuration

- Environment `development`: no protection rules.
- Environment `production`: required reviewers, restricted to the `main` branch.
- Per-environment variables `AWS_ROLE_ARN`, `AWS_CLOUDFORMATION_ROLE_ARN`,
  `AWS_STACK_NAME`; repository-level `AWS_REGION` and `AWS_REPOSITORY_URI`.

Naming, stated once to avoid ambiguity: GitHub environments are `development`
and `production`; AWS resource name suffixes are `dev` and `prod`. So the
`production` GitHub environment supplies the role for the
`interview-share-canvas-prod` stack. `EnvironmentName` in the templates always
takes the AWS form (`dev`, `prod`).

## Unverified detail

GitHub documents the immutable-ID subject format
(`repo:owner@id/repo@id:ref:refs/heads/main`, which this repository uses and
which is known to work) and separately documents the environment subject format
(`repo:owner/repo:environment:prod`). It shows no example combining them, and
an incorrect subject is what caused the first pipeline run to fail.

Therefore the first implementation step is a throwaway diagnostic job that
requests the Actions OIDC token and prints **only its `sub` claim**, never the
token. Both roles are then bound to the observed value. No role is written
against a guessed subject.

## Migration sequence

Ordered so that nothing existing is destroyed until its replacement is verified.

1. Deploy the registry stack. Confirm the repository exists.
2. Create the GitHub `development` and `production` environments, with the
   production reviewer rule. These must exist before step 3, because an
   environment subject claim cannot be observed until a job can run in that
   environment.
3. Run the subject-claim diagnostic in each environment; record both actual
   `sub` values.
4. Deploy the dev and prod OIDC stacks using the observed subjects.
5. Set the per-environment role ARN variables from the stack outputs.
6. Update the app template. Deploy `interview-share-canvas-dev` alongside the
   still-running old stack. **This deploy publishes the first image to the
   shared repository**, which prod cannot do, since the prod role has no ECR
   write permission.
7. Deploy `interview-share-canvas-prod` using that same image tag.
8. Verify both environments are healthy and independent.
9. Only then: delete stack `interview-share-canvas`, which snapshots its data
   volume and retains its ECR repository.
10. Delete the orphaned `interview-share-canvas` repository and the old OIDC
    stack once the new pipeline has completed a green end-to-end run.

Dev is therefore built before prod, because the shared repository starts empty
and only the dev role can populate it.

Steps 9 and 10 are destructive and are confirmed with the user at the time,
separately from this design's approval.

## Verification

Infrastructure cannot be unit tested, so each claim gets an explicit check:

| Claim | Check |
| --- | --- |
| Templates are valid | `aws cloudformation validate-template` on all three |
| Existing tests still pass | `uv run pytest`, frontend tests, integration and E2E suites |
| Both environments are healthy | `/health` returns `{"status":"ok"}` on each |
| The environments are genuinely independent | Instance IDs, volume IDs, and Elastic IPs are all distinct |
| Prod runs the tested artifact | The image digest deployed to prod equals the one dev deployed |
| Prod cannot publish images | `simulate-principal-policy` returns deny for `ecr:PutImage` on the prod role |
| Neither role can reach the other's stack | `simulate-principal-policy` returns deny for cross-environment `cloudformation:CreateChangeSet` and `ssm:SendCommand` |
| The approval gate works | A pipeline run stops at `deploy-prod` until approved |
| Old data was not lost | The retired stack's volume snapshot exists |

## Rollback

Until step 9, the original stack is untouched and still serving, so rollback is
deleting the new stacks. After step 9, the dev environment is restorable from
the volume snapshot taken at deletion. Production is unaffected by any of this,
because it does not exist until step 7.

## Follow-ups, not in this change

1. **TLS and a domain.** Both environments serve plain HTTP with an IP address
   for a hostname. This should be resolved before production carries real
   interview content.
2. Backups of the production data volume. `DeletionPolicy: Snapshot` protects
   against stack deletion, not against data corruption.
3. **Database parity.** Compose, the integration suite, and the Playwright test
   all run PostgreSQL, while both AWS environments run SQLite on EBS. Managed
   Postgres was evaluated on 2026-08-30 and deliberately not adopted, to avoid
   enlarging this change. The gap remains: the engine under test is not the
   engine in production. Revisit before the schema grows, since
   `Base.metadata.create_all` in `backend/store.py` creates missing tables but
   never alters existing ones.
3. Deleting the retired repository and OIDC stack, per step 10.

## Cost

Roughly $13/month for the second environment: one `t3.micro`, 22 GB of gp3 EBS,
and one Elastic IP.

# GitLab Runner IAM Permissions

The Relay pipeline (`.gitlab-ci.yml`) deploys with **no AWS access keys** — it relies on
the IAM **instance role** (or ECS task role) of the GitLab runner that lives in the target
AWS account. That role needs permission to create everything the chosen stacks provision
*plus* the CDK deploy machinery (CloudFormation, the bootstrap S3 asset bucket, etc.).

A runner **cannot grant itself permissions**, so a team/account administrator must attach
the appropriate policy below to the runner's role **once**, in the IAM console, before the
first deploy.

- Deploying a **team** topology (one always-on container + data plane, single account — the
  default) → attach the **Team deploy** policy.
- Deploying a **federated-hub** (the org-wide aggregator) → attach the **Federated-hub deploy**
  policy (the team policy plus the EventBridge bus permissions).

> **Replace `<ACCOUNT_ID>`** in the IAM statements below with the account ID of the account
> the runner is in (the same account you're deploying into). Region is left as `*` so the
> policy works in any region; tighten if you deploy to a fixed region.

---

## How to attach (IAM console)

1. Find the runner's role: **EC2 → Instances → (runner instance) → Security → IAM role**
   (or **ECS → Task definition → Task role** if the runner is containerized).
2. **IAM → Roles → (that role) → Add permissions → Create inline policy**.
3. Choose the **JSON** tab, paste the relevant policy below, replace `<ACCOUNT_ID>`.
4. Name it `relay-team-deploy` or `relay-hub-deploy` and create.

---

## A note on least privilege

These are **deploy-time** policies — they are intentionally broad on the services each
stack touches because CloudFormation creates, updates, and deletes those resources. Two
ways to tighten:

- **Pre-bootstrap once as an admin.** If an administrator runs `cdk bootstrap` in the
  account beforehand, the runner role can be reduced to: assume the `cdk-hnb659fds-*`
  roles, `cloudformation:*` on the stack, `s3` on the assets bucket, and `ssm:GetParameter`
  on `/cdk-bootstrap/*`. The CDK bootstrap roles then do the resource creation. This is the
  tightest model and is recommended for production. (Then remove the `iam:CreateRole` /
  `ec2:*` breadth from the policies below.)
- **Scope by resource name.** Relay names its resources `relay-*`; where a service supports
  resource-level permissions you can constrain ARNs to `relay-*`. The IAM statements below
  already do this for `iam:*` role actions (the most sensitive), restricting them to
  `relay-*` and `cdk-*` roles.

Have your security team review before using in production.

---

## Team deploy policy

A team deploy provisions **RelayDataStack** (DynamoDB table + GSI + stream + paging SNS
topics) and **RelayComputeStack** (VPC, ECS cluster, always-on Fargate service + ALB,
application auto-scaling, the CloudWatch-alarm EventBridge rule → SQS ingress queue + DLQ,
and one task role + one execution role). There is no Lambda and no EventBridge Scheduler.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CdkDeployMachinery",
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:PutParameter",
        "ssm:DeleteParameter"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CdkAssetsBucket",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:PutBucketPolicy",
        "s3:PutBucketVersioning",
        "s3:PutEncryptionConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::cdk-*",
        "arn:aws:s3:::cdk-*/*"
      ]
    },
    {
      "Sid": "ContainerImageEcr",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DescribeRepositories",
        "ecr:SetRepositoryPolicy",
        "ecr:PutLifecyclePolicy",
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RelayIamRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:CreateServiceLinkedRole",
        "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:iam::<ACCOUNT_ID>:role/relay-*",
        "arn:aws:iam::<ACCOUNT_ID>:role/cdk-*",
        "arn:aws:iam::<ACCOUNT_ID>:role/aws-service-role/*"
      ]
    },
    {
      "Sid": "RelayNetworking",
      "Effect": "Allow",
      "Action": [
        "ec2:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RelayComputeAndData",
      "Effect": "Allow",
      "Action": [
        "ecs:*",
        "elasticloadbalancing:*",
        "application-autoscaling:*",
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DeleteAlarms",
        "cloudwatch:DescribeAlarms",
        "dynamodb:CreateTable",
        "dynamodb:DeleteTable",
        "dynamodb:DescribeTable",
        "dynamodb:UpdateTable",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:UpdateTimeToLive",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:UpdateContinuousBackups",
        "dynamodb:CreateGlobalSecondaryIndex",
        "dynamodb:DescribeStream",
        "dynamodb:TagResource",
        "dynamodb:UntagResource",
        "dynamodb:ListTagsOfResource",
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:GetTopicAttributes",
        "sns:SetTopicAttributes",
        "sns:TagResource",
        "sns:UntagResource",
        "sqs:CreateQueue",
        "sqs:DeleteQueue",
        "sqs:GetQueueAttributes",
        "sqs:SetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ListQueues",
        "sqs:TagQueue",
        "sqs:UntagQueue",
        "events:PutRule",
        "events:DeleteRule",
        "events:DescribeRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "events:TagResource",
        "events:UntagResource",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:DescribeLogGroups",
        "logs:TagResource",
        "logs:TagLogGroup"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Federated-hub deploy policy

A federated-hub deploy provisions the same data + compute stacks as a team **plus**
**RelayFederationStack** — the `relay-hub` EventBridge bus, its org-scoped `PutEvents`
resource policy, and the ingest rule that team containers forward SEV1/SEV2 incidents up to.

Attach the **Team deploy** policy above, then add the EventBridge-bus statement below
(or merge it into the same inline policy).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RelayFederationBus",
      "Effect": "Allow",
      "Action": [
        "events:CreateEventBus",
        "events:DeleteEventBus",
        "events:DescribeEventBus",
        "events:PutPermission",
        "events:RemovePermission",
        "events:PutRule",
        "events:DeleteRule",
        "events:DescribeRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "events:TagResource",
        "events:UntagResource"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Verifying

After attaching, the team can confirm by running the pipeline's **synth** job (no AWS writes)
and then the manual **deploy** job. If the runner lacks a permission, CloudFormation fails
with an `AccessDenied` naming the exact missing action — add it to the inline policy and
retry. (This is the normal way to tighten these policies toward least privilege: start from
the above, deploy, and trim anything unused.)

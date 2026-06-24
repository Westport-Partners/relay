

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Using migration runbooks for OpsCenter
<a name="migration-opscenter-runbooks"></a>

This guide provides step-by-step instructions for migrating your Amazon CloudWatch alarms and Amazon EventBridge rules from AWS Systems Manager Incident Manager to AWS Systems Manager OpsCenter using automated migration runbooks.

For an overview of OpsCenter capabilities and to understand the differences between Incident Manager and OpsCenter, see [Migrating to AWS Systems Manager OpsCenter](migration-opscenter.md).

## Migration overview
<a name="migration-overview"></a>

The migration process uses [Systems Manager Automation](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation.html) runbooks to integrate your existing CloudWatch alarms and EventBridge rules with OpsCenter. The process includes the following steps:
+ **Deploy infrastructure** - Deploy the CloudFormation stack to create required resources for the migration runbooks.
+ **Migrate CloudWatch alarms and EventBridge rules** - Run the automation runbooks to migrate your resources to OpsCenter.
+ **Clean up resources** - Optionally delete the Replication Set and other Incident Manager resources.

**Note**  
The runbooks support migration for a single account-region pair. If you have resources across multiple accounts or regions, you must execute the migration separately for each account-region combination.

## Step 1: Deploy the CloudFormation template
<a name="deploy-cloudformation-template"></a>

Deploy the CloudFormation template to create the IAM role, Amazon S3 bucket, and Amazon SNS topic required by the migration runbooks.

### Required IAM permissions
<a name="required-iam-permissions"></a>

To deploy this CloudFormation template, you need IAM permissions for CloudFormation stack operations (`cloudformation:CreateStack`, `cloudformation:DescribeStacks`), IAM role management (`iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:PassRole`), Amazon S3 bucket creation and configuration (`s3:CreateBucket`, `s3:PutBucket*`), and Amazon SNS topic operations (`sns:CreateTopic`, `sns:Subscribe`, `sns:SetTopicAttributes`).

For complete details on CloudFormation permissions, see [CloudFormation permissions reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-iam-template.html) in the CloudFormation User Guide.

### To deploy the CloudFormation template using the console
<a name="deploy-console"></a>

1. Download and extract the [AWS-IncidentManager-MigrationResources.zip](./samples/AWS-IncidentManager-MigrationResources.zip) file that contains the `AWS-IncidentManager-MigrationResources.yaml` template.

1. Open the CloudFormation console at [https://console.aws.amazon.com/cloudformation](https://console.aws.amazon.com/cloudformation).

1. Choose **Create stack**.

1. In the **Specify template** section, choose **Upload a template file**.

1. Choose **Choose file**, and then select the `AWS-IncidentManager-MigrationResources.yaml` file.

1. Choose **Next**.

1. On the **Specify stack details** page, enter the following:
   + **Stack name** - Enter a name (for example, `im-migration-infrastructure`)
   + **ApprovalEmail** - Enter the email address to receive approval notifications (only used when the RequireManualApproval runbook parameter is set to true).
   + **IsPrimaryMigrationRegion** - Choose `true` if this is the first region in your account where you're deploying the stack, otherwise choose `false`

1. Choose **Next**.

1. On the **Configure stack options** page, choose **Next**.

1. On the **Review** page, scroll down and select **I acknowledge that CloudFormation might create IAM resources with custom names**.

1. Choose **Submit**.

CloudFormation displays the `CREATE_IN_PROGRESS` status. The status changes to `CREATE_COMPLETE` when the stack is ready.

**Note**  
If you have CloudWatch alarms or EventBridge rules in multiple regions, deploy this CloudFormation stack in each region where you want to perform the migration.  
For multi-account deployments across AWS Organizations, use two CloudFormation StackSets:  
**Primary StackSet** - Set IsPrimaryMigrationRegion to true for one region per account
**Secondary StackSet** - Set IsPrimaryMigrationRegion to false for all other regions
  
For instructions, see [Working with CloudFormation StackSets](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/what-is-cfnstacksets.html) in the CloudFormation User Guide.

### To deploy the CloudFormation template using the AWS CLI
<a name="deploy-cli"></a>

For the first region in your account, use the following command:

```
aws cloudformation create-stack \
    --stack-name im-migration-infrastructure \
    --template-body file://AWS-IncidentManager-MigrationResources.yaml \
    --parameters ParameterKey=ApprovalEmail,ParameterValue=your-email@example.com \
    ParameterKey=IsPrimaryMigrationRegion,ParameterValue=true \
    --capabilities CAPABILITY_NAMED_IAM \
    --region us-east-1
```

For additional regions in the same account, set `IsPrimaryMigrationRegion` to `false`:

```
aws cloudformation create-stack \
    --stack-name im-migration-infrastructure \
    --template-body file://AWS-IncidentManager-MigrationResources.yaml \
    --parameters ParameterKey=ApprovalEmail,ParameterValue=your-email@example.com \
    ParameterKey=IsPrimaryMigrationRegion,ParameterValue=false \
    --capabilities CAPABILITY_NAMED_IAM \
    --region us-west-2
```

To verify the stack status:

```
aws cloudformation describe-stacks \
    --stack-name im-migration-infrastructure \
    --query 'Stacks[0].StackStatus' \
    --output text
```

Wait until the command returns `CREATE_COMPLETE` before proceeding to the next step.

## Step 2: Migrate CloudWatch alarms and EventBridge rules
<a name="migrate-resources"></a>

Use the Systems Manager Automation runbooks to migrate your CloudWatch alarms and EventBridge rules from Incident Manager to OpsCenter.

### Migration runbooks
<a name="migration-runbooks-overview"></a>
+ [AWS-MigrateIncidentManagerCloudWatchAlarms](https://console.aws.amazon.com/systems-manager/documents/AWS-MigrateIncidentManagerCloudWatchAlarms)
+ [AWS-MigrateIncidentManagerEventBridgeRules](https://console.aws.amazon.com/systems-manager/documents/AWS-MigrateIncidentManagerEventBridgeRules)

For more information about what these runbooks do, including detailed step descriptions, input parameters, and outputs, see the runbook documentation.

### How the runbooks work
<a name="how-runbooks-work"></a>

Both migration runbooks follow the same workflow:
+ **Discovery and batching** - Discovers all CloudWatch alarms or EventBridge rules configured with Incident Manager response plan actions and organizes them into configurable batches.
+ **Manual approval (optional)** - By default, requires explicit approval before proceeding with migration, with a 24-hour timeout. An Amazon SNS notification is sent to the email address specified during CloudFormation deployment. All configurations are backed up to Amazon S3, and the complete list of resources to be migrated is stored for manual review. This step can be skipped by setting RequireManualApproval to false.
+ **Backup and migration** - If manual approval is set to true, waits for approval then proceeds to back up each configuration to Amazon S3 and performs the migration. If set to false, proceeds directly to backup and migration.

### Input parameters
<a name="input-parameters"></a>

Both runbooks require the following parameters:

AutomationAssumeRole (Required)  
The ARN of the `IM-Migration-Automation-Role` created by the CloudFormation stack.

ApproverArn (Required)  
The ARN of the IAM role or user who can review and approve the migration.

S3BucketName (Required)  
The name of the Amazon S3 bucket created by the CloudFormation stack.

SNSTopicArn (Required)  
The ARN of the Amazon SNS topic created by the CloudFormation stack.

MaxNumberOfAlarmsToMigrate or MaxNumberOfRulesToMigrate (Optional)  
The maximum number of resources to migrate in a single execution. Valid values: 1, 5, 10, 50, 100, 500, 5000, 10000, 25000, 50000. Default: 10000.

BatchSize (Optional)  
The number of resources to process in each batch. Valid values: 25, 50, 100, 200, 250, 300, 350, 400, 450, 500. Default: 100. The runbook supports a maximum of 100 × BatchSize resources per execution.

RequireManualApproval (Optional)  
Boolean value to control whether manual approval is required before migration. When set to true (default), you receive an Amazon SNS notification email with the Amazon S3 location of the resource list and a link to the automation execution console to approve, deny, or cancel. When set to false, the runbook proceeds automatically after discovery and backup. Valid values: true, false. Default: true.

### To migrate using the console
<a name="migrate-console"></a>

1. Open the Systems Manager console at [https://console.aws.amazon.com/systems-manager](https://console.aws.amazon.com/systems-manager).

1. In the navigation pane, choose **Automation**.

1. Search for the runbook name (`AWS-MigrateIncidentManagerCloudWatchAlarms` or `AWS-MigrateIncidentManagerEventBridgeRules`).

1. Choose **Execute automation**.

1. Enter the parameter values from your CloudFormation stack outputs.

1. (Optional) Set **RequireManualApproval** to `false` if you want to skip the manual approval step.

1. Choose **Execute**.

1. If `RequireManualApproval` is set to true (default), you receive an email notification when execution awaits manual review. The email contains an approval link to the automation execution console page. Review the resource list in the Amazon S3 bucket, then approve, deny, or cancel within 24 hours from either the email link or the console page. Migration only proceeds after approval. If set to false, migration proceeds automatically after backup.

1. Wait for the execution status to change to **Success**.

### To migrate using the AWS CLI
<a name="migrate-cli"></a>

**For CloudWatch alarms:**

```
aws ssm start-automation-execution \
    --document-name "AWS-MigrateIncidentManagerCloudWatchAlarms" \
    --parameters '{
        "AutomationAssumeRole": ["arn:aws:iam::123456789012:role/IM-Migration-Automation-Role"],
        "ApproverArn": ["arn:aws:iam::123456789012:role/Admin"],
        "S3BucketName": ["im-migration-logs-123456789012-us-east-1"],
        "SNSTopicArn": ["arn:aws:sns:us-east-1:123456789012:Automation-IM-Migration-Approvals"],
        "RequireManualApproval": ["false"]
    }' \
    --region us-east-1
```

**For EventBridge rules:**

```
aws ssm start-automation-execution \
    --document-name "AWS-MigrateIncidentManagerEventBridgeRules" \
    --parameters '{
        "AutomationAssumeRole": ["arn:aws:iam::123456789012:role/IM-Migration-Automation-Role"],
        "ApproverArn": ["arn:aws:iam::123456789012:role/Admin"],
        "S3BucketName": ["im-migration-logs-123456789012-us-east-1"],
        "SNSTopicArn": ["arn:aws:sns:us-east-1:123456789012:Automation-IM-Migration-Approvals"],
        "RequireManualApproval": ["false"]
    }' \
    --region us-east-1
```

To review the resource list in Amazon S3:

```
# For CloudWatch alarms
aws s3 cp s3://im-migration-logs-123456789012-us-east-1/review/CloudWatch/review_CW_alarms_to_migrate_123456789012_us-east-1.json ./

# For EventBridge rules
aws s3 cp s3://im-migration-logs-123456789012-us-east-1/review/EventBridge/review_EB_rules_to_migrate_123456789012_us-east-1.json ./
```

If RequireManualApproval is set to true, review the resource list and approve the migration by clicking the approval link in the email notification or from the automation execution console page. If set to false, the migration proceeds automatically after backup.

## Step 3: Verify your migration
<a name="verify-migration"></a>

After completing the migration, verify that your resources are functioning correctly:
+ **Trigger a test alarm or event** - Activate one of your migrated CloudWatch alarms or EventBridge rules to generate a test notification.
+ **Confirm OpsItem creation** - Verify that an OpsItem is automatically created in OpsCenter when the alarm or event triggers.
+ **Validate severity mapping** - Check that the severity level from your original Incident Manager configuration is correctly preserved in the OpsItem. (Applicable only to CloudWatch alarms).

## Step 4: Clean up Incident Manager resources
<a name="cleanup-resources"></a>

After successfully migrating your CloudWatch alarms and EventBridge rules, you can optionally clean up Incident Manager resources to fully off-board from the service.

For detailed instructions on deleting the Replication Set, response plans, contacts, runbooks, and other Incident Manager resources, see [Cleaning up Incident Manager Resources](migration-cleanup.md).

### Delete CloudFormation stacks (optional)
<a name="delete-cloudformation-stacks"></a>

You can delete the CloudFormation stacks to remove the IAM role, Amazon SNS topic, and Amazon S3 bucket created for the migration.

**Important**  
The Amazon S3 bucket containing backups of all the migrated resources must be emptied before stack deletion. CloudFormation cannot delete Amazon S3 buckets that contain objects.

**To delete the CloudFormation stack**

```
aws cloudformation delete-stack --stack-name <your-stack-name>
```

## Monitoring and troubleshooting
<a name="monitoring-troubleshooting"></a>

**CloudWatch Logs** - Migration activities are logged to CloudWatch Logs:
+ CloudWatch alarms: `/aws/ssm/incidentmanager/cwmigration`
+ EventBridge rules: `/aws/ssm/incidentmanager/ebmigration`

**Amazon S3 backup structure** - All configurations are backed up to Amazon S3 before migration:

```
migration-logs-{AccountId}-{Region}/
├── backups/
│   ├── CloudWatch/
│   │   └── {AccountId}/
│   │       └── {Region}/
│   │           └── {AlarmName}_backup.json
│   └── EventBridge/
│       └── {AccountId}/
│           └── {Region}/
│               └── {RuleName}_backup.json
└── review/
    ├── CloudWatch/
    │   └── review_CW_alarms_to_migrate_{AccountId}_{Region}.json
    └── EventBridge/
        └── review_EB_rules_to_migrate_{AccountId}_{Region}.json
```

**Common issues:**
+ **Amazon SNS notification not received** (when RequireManualApproval=true) - Check the Amazon SNS topic subscription:

  ```
  aws sns list-subscriptions-by-topic --topic-arn <sns-topic-arn>
  ```
+ **Partial migration failures** - Check CloudWatch Logs for detailed error messages and retry the automation with a reduced batch size.

**Rollback procedure:**

If you need to roll back the migration:
+ Retrieve backups from Amazon S3:

  ```
  aws s3 sync s3://im-migration-logs-123456789012-us-east-1/backups/ ./backups/
  ```
+ Restore resources:

  ```
  # For CloudWatch alarms
  aws cloudwatch put-metric-alarm --cli-input-json file://backups/CloudWatch/123456789012/us-east-1/MyAlarm_backup.json
  
  # For EventBridge rules
  aws events put-targets --rule MyRule --targets file://backups/EventBridge/123456789012/us-east-1/MyRule_backup.json
  ```

## Frequently asked questions
<a name="faq"></a>

Q: What happens if the automation times out during approval?  
A: The automation times out after 24 hours if no approval is received. You can restart the automation with the same parameters.

Q: Can I migrate resources across regions?  
A: No. Each region must be migrated separately using region-specific automation executions.

Q: How long does migration take?  
A: Migration time depends on the number of resources:  
+ \~100 alarms/rules: 5-10 minutes
+ \~1000 alarms/rules: 30-60 minutes
+ \~10000 alarms/rules: 2-4 hours

Q: Is the severity preserved after migration to OpsCenter?  
A: Yes. The severity configured in the Incident Manager response plan impact levels is preserved and automatically mapped to appropriate OpsCenter severity levels during CloudWatch alarm migration. This does not apply to EventBridge rules.

Q: Will I be charged for executing the automation runbooks?  
A: No. The migration automation runbooks do not incur execution charges. However, OpsCenter usage after migration will incur charges. For details, see the [Systems Manager pricing](https://aws.amazon.com/systems-manager/pricing/) documentation.

## Related resources
<a name="related-resources-runbooks"></a>
+ [Migrating to AWS Systems Manager OpsCenter](migration-opscenter.md)
+ [AWS Systems Manager OpsCenter User Guide](https://docs.aws.amazon.com/systems-manager/latest/userguide/OpsCenter.html)
+ [Systems Manager Automation](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation.html)
+ [Exporting Incident Manager data](export-data.md)
+ [Cleaning up Incident Manager Resources](migration-cleanup.md)
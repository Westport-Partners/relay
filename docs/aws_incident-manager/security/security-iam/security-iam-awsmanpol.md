

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# AWS managed policies for AWS Systems Manager Incident Manager
<a name="security-iam-awsmanpol"></a>



An AWS managed policy is a standalone policy that is created and administered by AWS. AWS managed policies are designed to provide permissions for many common use cases so that you can start assigning permissions to users, groups, and roles.

Keep in mind that AWS managed policies might not grant least-privilege permissions for your specific use cases because they're available for all AWS customers to use. We recommend that you reduce permissions further by defining [ customer managed policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_managed-vs-inline.html#customer-managed-policies) that are specific to your use cases.

You cannot change the permissions defined in AWS managed policies. If AWS updates the permissions defined in an AWS managed policy, the update affects all principal identities (users, groups, and roles) that the policy is attached to. AWS is most likely to update an AWS managed policy when a new AWS service is launched or new API operations become available for existing services.

For more information, see [AWS managed policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_managed-vs-inline.html#aws-managed-policies) in the *IAM User Guide*.









## AWS managed policy: AWSIncidentManagerIncidentAccessServiceRolePolicy
<a name="security-iam-awsmanpol-AWSIncidentManagerIncidentAccessServiceRolePolicy"></a>





You can attach `AWSIncidentManagerIncidentAccessServiceRolePolicy` to your IAM entities. Incident Manager also attaches this policy to an Incident Manager role that allows Incident Manager to perform actions on your behalf. 



This policy grants read-only permissions that allow Incident Manager to read resources in certain other AWS services to identify findings related to incidents in those services.



**Permissions details**

This policy includes the following permissions.




+ `cloudformation` – Allows principals to describe CloudFormation stacks. This is required for Incident Manager to identify CloudFormation events and resources related to an incident.
+ `codedeploy` – Allows principals to read AWS CodeDeploy deployments. This is required for Incident Manager to identify CodeDeploy deployments and targets related to an incident.
+ `autoscaling` – Allows principals to determine if an Amazon Elastic Compute Cloud (EC2) instance is part of an Auto Scaling group. This is needed so Incident Manager can provide findings for EC2 instances that are part of Auto Scaling groups.



To view more details about the policy, including the latest version of the JSON policy document, see [https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIncidentManagerIncidentAccessServiceRolePolicy.html](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIncidentManagerIncidentAccessServiceRolePolicy.html) in the *AWS Managed Policy Reference Guide*.

## AWS managed policy: `AWSIncidentManagerServiceRolePolicy`
<a name="security-iam-awsmanpol-AWSServiceRoleforIncidentManagerPolicy"></a>



You can't attach `AWSIncidentManagerServiceRolePolicy` to your IAM entities. This policy is attached to a service-linked role that allows Incident Manager to perform actions on your behalf. For more information, see [Using service-linked roles for Incident Manager](using-service-linked-roles.md).



This policy grants Incident Manager permissions to list incidents, create timeline events, create OpsItems, associate related items to OpsItems, start engagements, and publish CloudWatch metrics related to an incident.



**Permissions details**

This policy includes the following permissions.




+ `ssm-incidents` – Allows principals to list incidents and create timeline events. This is required so responders can collaborate during an incident on the incident dashboard.
+ `ssm` – Allows principals to create OpsItems and associate related items. This is required to create a parent OpsItem when an incident starts.
+ `ssm-contacts` – Allows principals to start engagements. This is required for Incident Manager to engage contacts during an incident.
+ `cloudwatch` – Allows principals to publish CloudWatch metrics. This is required for Incident Manager to publish metrics related to an incident and usage metrics.



To view more details about the policy, including the latest version of the JSON policy document, see [https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIncidentManagerServiceRolePolicy.html](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIncidentManagerServiceRolePolicy.html) in the *AWS Managed Policy Reference Guide*.

## AWS managed policy: `AWSIncidentManagerResolverAccess`
<a name="security-iam-awsmanpol-AWSIncidentManagerResolverAccess"></a>



You can attach `AWSIncidentManagerResolverAccess` to your IAM entities to allow them to start, view, and update incidents. This also allows them to create customer timeline events and related items in the incident dashboard. You can also attach this policy to the Amazon Q Developer in chat applications service role or directly to your customer managed role associated with any chat channel used for incident collaboration. To learn more about IAM policies in Amazon Q Developer in chat applications, see [Managing permissions for running commands using Amazon Q Developer in chat applications](https://docs.aws.amazon.com/chatbot/latest/adminguide/chatbot-cli-commands.html#iam-policies-for-slack-channels-cli-support) in the *Amazon Q Developer in chat applications Administrator Guide*.

**Permissions details**

This policy includes the following permissions.




+ `ssm-incidents` – Allows principals to start incidents, list response plans, list incidents, update incidents, list timeline events, create custom timeline events, update custom timeline events, delete custom timeline events, list related items, create related items, and update related items.
+ `ssm-contacts` – Allows principals to start engagements with contacts during incident creation.



To view more details about the policy, including the latest version of the JSON policy document, see [https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIncidentManagerResolverAccess.html](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIncidentManagerResolverAccess.html) in the *AWS Managed Policy Reference Guide*.





## Incident Manager updates to AWS managed policies
<a name="security-iam-awsmanpol-updates"></a>



View details about updates to AWS managed policies for Incident Manager since this service began tracking these changes. For automatic alerts about changes to this page, subscribe to the RSS feed on the Incident Manager Document history page.




| Change | Description | Date | 
| --- | --- | --- | 
| [`AWSIncidentManagerResolverAccess`](#security-iam-awsmanpol-AWSIncidentManagerResolverAccess) – Policy update | Incident Manager added permission to start engagements with contacts. | November 20, 2025 | 
| [`AWSIncidentManagerServiceRolePolicy`](#security-iam-awsmanpol-AWSServiceRoleforIncidentManagerPolicy) – Policy update | Incident Manager added a new permission that allows Incident Manager to publish metrics within the `AWS/Usage` namespace into your account. | January 27, 2025 | 
| [AWSIncidentManagerIncidentAccessServiceRolePolicy](#security-iam-awsmanpol-AWSIncidentManagerIncidentAccessServiceRolePolicy) – Policy update | Incident Manager has added a new permission to AWSIncidentManagerIncidentAccessServiceRolePolicy, in support of the Findings feature, that allows it to check whether an EC2 instance is part of an Auto Scaling group. | February 20, 2024 | 
| [`AWSIncidentManagerIncidentAccessServiceRolePolicy`](#security-iam-awsmanpol-AWSIncidentManagerIncidentAccessServiceRolePolicy) – New policy | Incident Manager added a new policy that grants Incident Manager permissions to call other AWS services as a part of managing an incident. | November 17, 2023 | 
| [`AWSIncidentManagerServiceRolePolicy`](#security-iam-awsmanpol-AWSServiceRoleforIncidentManagerPolicy) – Policy update | Incident Manager added a new permission that allows Incident Manager to publish metrics into your account. | Dec 16, 2022 | 
| [`AWSIncidentManagerResolverAccess`](#security-iam-awsmanpol-AWSIncidentManagerResolverAccess) – New policy | Incident Manager added a new policy to allow you to start incidents, list response plans, list incidents, update incidents, list timeline events, create custom timeline events, update custom timeline events, delete custom timeline events, list related items, create related items, and update related items. | April 26, 2021 | 
| [`AWSIncidentManagerServiceRolePolicy`](#security-iam-awsmanpol-AWSServiceRoleforIncidentManagerPolicy) – New policy | Incident Manager added a new policy to grant Incident Manager permissions to list incidents, create timeline events, create OpsItems, associate related items to OpsItems, and start engagements related to an incident. | April 26, 2021 | 
| Incident Manager started tracking changes | Incident Manager started tracking changes for its AWS managed policies. | April 26, 2021 | 
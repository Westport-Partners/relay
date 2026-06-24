

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Setting up AWS Systems Manager Incident Manager
<a name="setting-up"></a>

We recommend setting up AWS Systems Manager Incident Manager in the account that you use to manage your operations. Before you use Incident Manager for the first time, complete the following tasks:

**Topics**
+ [Sign up for an AWS account](#sign-up-for-aws)
+ [Required role for Incident Manager setup](#iam-prereq-service-role)

## Sign up for an AWS account
<a name="sign-up-for-aws"></a>

To get started with AWS, you need an AWS account. For information about creating an AWS account, see [Getting started with an AWS account](https://docs.aws.amazon.com//accounts/latest/reference/getting-started.html) in the *AWS Account Management Reference Guide*.

## Required role for Incident Manager setup
<a name="iam-prereq-service-role"></a>

Before you begin, your account must have the IAM permission `iam:CreateServiceLinkedRole`. Incident Manager uses this permission to create the `AWSServiceRoleforIncidentManager` in your account. For more information, see [Using service-linked roles for Incident Manager](using-service-linked-roles.md). 


AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Identifying potential causes of incidents from other services as "findings" in Incident Manager
<a name="findings"></a>

In Incident Manager, a *finding* is information about an AWS CodeDeploy deployments or AWS CloudFormation stack update that occurred around the time of an incident, and that involved one or more resources likely related to the incident. Each finding can be examined as a potential cause of the incident. Information about these potential causes is added to the **Incident details** page for an incident. With information about these deployments and changes readily at hand, responders don't need to manually search for this information. This lessens the time needed to evaluate potential causes, which can reduce the mean time to recover (MTTR) from an incident.

Currently, Incident Manager supports gathering findings from two AWS services: [AWS CodeDeploy](https://docs.aws.amazon.com/codedeploy/latest/userguide) and [AWS CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/).

Findings is an opt-in feature. You can enable it in the [**Get prepared** wizard](getting-started.md#getting-started-wizard), when you are first onboarding to Incident Manager, or later on the [**Settings** page](general-settings.md#settings-findings).

When you enable the Findings feature, Incident Manager creates a service role for you. This service role includes the permissions needed to retrieve findings from CodeDeploy and CloudFormation. 

To work with findings in a cross-account scenario, enable the feature in the management account. After that, each application account in an AWS Resource Access Manager (AWS RAM) organization must create a corresponding service role.

Refer to the following topics to help you use the Findings feature.

**Topics**
+ [Enable and create a service role for findings](#create-findings-role)
+ [Configure permissions for cross-account findings support](#findings-role-permissions)

## Enable and create a service role for findings
<a name="create-findings-role"></a>

When you enable the Findings feature, Incident Manager creates a service role named `IncidentManagerIncidentAccessServiceRole` on your behalf. This service role provides the permissions Incident Manager needs to gather information about CodeDeploy deployments and CloudFormation stack updates that occurred around the time an incident was created.

**Note**  
If you are using Incident Manager with an organization, the service role is created in the management account. To work with findings across other accounts in the organization, the service role must be created in each application account. For information about using a CloudFormation template to create this role in your application accounts, see step 4 in [Set up and configure cross-account incident management](incident-manager-cross-account-cross-region.md#cross-account-cross-region-setup).

This service role is associated with an AWS managed policy. For information about the permissions in this policy, see [AWS managed policy: AWSIncidentManagerIncidentAccessServiceRolePolicy](security-iam-awsmanpol.md#security-iam-awsmanpol-AWSIncidentManagerIncidentAccessServiceRolePolicy).

For information about enabling findings during the Incident Manager onboarding process, see [Getting started with Incident Manager](getting-started.md). 

For information about enabling findings after you have completed the onboarding process, see [Managing the Findings feature](general-settings.md#settings-findings).

## Configure permissions for cross-account findings support
<a name="findings-role-permissions"></a>

To use the Findings feature across accounts with an organization set up in AWS RAM, each application account must configure permissions for Incident Manager to assume the management account's service role on its behalf. 

These permissions can be configured in an application account by deploying an CloudFormation template provided by AWS, which creates the role `IncidentManagerIncidentAccessServiceRole`. 

For information about downloading and deploying this template in an application account, see step 4 in [Managing incidents across AWS accounts and Regions in Incident Manager](incident-manager-cross-account-cross-region.md).
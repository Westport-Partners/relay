

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Integrating Systems Manager Automation runbooks in Incident Manager for incident remediation
<a name="runbooks"></a>

You can use runbooks from [AWS Systems Manager Automation](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation.html), a tool in AWS Systems Manager, to automate common application and infrastructure tasks in your AWS Cloud environment.

Each runbook defines a *runbook workflow*, which is composed of the actions that Systems Manager performs on your managed nodes or other AWS resource types. You can use runbooks to automate the maintenance, deployment, and remediation of your AWS resources.



In Incident Manager, a runbook drives incident response and mitigation, and you specify a runbook to use as part of a response plan.

In your response plans, you can choose from dozens of pre-configured runbooks for commonly automated tasks, or you can create custom runbooks. When you specify a runbook in a response plan definition, the system can automatically start the runbook when an incident starts.

**Important**  
Incidents created by a cross-Region failover don't invoke runbooks specified in response plans.

For more information about Systems Manager Automation, runbooks, and using runbooks with Incident Manager, see the following topics:
+ To add a runbook to a response plan, see [Creating and configuring response plans in Incident Manager](response-plans.md).
+ To learn more about runbooks, see [AWS Systems Manager Automation](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation.html) in the *AWS Systems Manager User Guide* and the *[AWS Systems Manager Automation runbook reference](https://docs.aws.amazon.com/systems-manager-automation-runbooks/latest/userguide/automation-runbook-reference.html)*.
+ For information about the cost of using runbooks, see [Systems Manager pricing](https://aws.amazon.com/systems-manager/pricing/).
+ For information about automatically invoking runbooks when an incident is created by a Amazon CloudWatch alarm or an Amazon EventBridge event, see [Tutorial: Using Systems Manager Automation runbooks with Incident Manager](https://docs.aws.amazon.com//incident-manager/latest/userguide/tutorials-runbooks.html).

**Topics**
+ [IAM permissions required to start and run runbook workflows](#runbook-permissions)
+ [Working with runbook parameters](#runbooks-parameters)
+ [Define a runbook](#runbook-create)
+ [Incident Manager runbook template](#runbooks-template)

## IAM permissions required to start and run runbook workflows
<a name="runbook-permissions"></a>

Incident Manager requires permissions to run runbooks as part of your incident response. To provide these permissions, you use AWS Identity and Access Management (IAM) roles, the *Runbook service role*, and the *Automation `AssumeRole`*.

The Runbook service role is a required service role. This role provides Incident Manager with the permissions it needs to access and start the workflow for the runbook. 

The Automation `AssumeRole` provides the permissions needed to run the individual commands specified within the runbook. 

**Note**  
If no `AssumeRole` is specified, Systems Manager Automation attempts to use the Runbook service role for individual commands. If you don't specify an `AssumeRole`, you must add the necessary permissions to the Runbook service role. If you don't, the runbook fails to run those commands.  
However, as a security best practice, we recommend using a separate `AssumeRole`. With a separate `AssumeRole`, you can limit the necessary permissions you must add to each role.

For more information about the Automation `AssumeRole`, see [Configuring a service role (assume role) access for automations](https://docs.aws.amazon.com/systems-manager/latest/userguide/automation-setup.html#automation-setup-configure-role) ' in the *AWS Systems Manager User Guide*.

You can create either type of role manually yourself in the IAM console.- You can also let Incident Manager create either one for you when you create or update a response plan.

**Runbook service role permissions**  
Runbook service role permissions are provided through a policy similar to the following. 

The first statement allows Incident Manager to start the Systems Manager `StartAutomationExecution` operation. This operation then runs on resources represented by the three Amazon Resource Name (ARN) formats.

The second statement allows the Runbook service role to assume a role in another account when that runbook runs in the impacted account. For more information, see [Running automations in multiple AWS Regions and accounts](https://docs.aws.amazon.com/systems-manager/latest/userguide/running-automations-multiple-accounts-regions.html) in the *AWS Systems Manager User Guide*.

------
#### [ JSON ]

****  

```
{
    "Version":"2012-10-17",		 	 	 
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "ssm:StartAutomationExecution",
            "Resource": [
                "arn:aws:ssm:*:{{111122223333}}:document/{{DocumentName}}",
                "arn:aws:ssm:*:{{111122223333}}:automation-execution/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": "arn:aws:iam::*:role/AWS-SystemsManager-AutomationExecutionRole",
            "Condition": {
                "StringEquals": {
                    "aws:CalledViaLast": "ssm.amazonaws.com"
                }
            }
        }
    ]
}
```

------

**Automation AssumeRole permissions**  
``When you create or update a response plan, you can choose from several AWS managed policies to attach to the AssumeRole that Incident Manager creates. These policies provide permissions to run a number of common operations used in Incident Manager runbook scenarios. You can choose one or more of these managed policies to provide permissions for your `AssumeRole` policy. ``The following table describes the policies that you can choose from when you create an `AssumeRole` from the Incident Manager console. 


| AWS managed policy name | Policy description | 
| --- | --- | 
| AmazonSSMAutomationRole | Grants permissions for the Systems Manager Automation service to run activities defined within runbooks. Assign this policy to administrators and trusted power users. | 
| AWSIncidentManagerResolverAccess | Grants permission for users to start, view, and update incidents. You can also use them to create customer timeline events and related items in the incident dashboard.  | 

You can use these managed policies to grant permissions for many common incident response scenarios. However, the permissions required for the specific tasks you need can vary. In these cases, you need to provide additional policy permissions for your `AssumeRole`. For information, see the *[AWS Systems Manager Automation runbook reference](https://docs.aws.amazon.com/systems-manager-automation-runbooks/latest/userguide/automation-runbook-reference.html)*.

## Working with runbook parameters
<a name="runbooks-parameters"></a>

When you add a runbook to a response plan, you can specify the parameters the runbook should use at runtime. Response plans support parameters with both static and dynamic values. For static values, you enter the value when you define the parameter in the response plan. For dynamic values, the system determines the correct parameter value by collecting information from the incident. Incident Manager supports the following dynamic parameters:

`Incident ARN`  
When Incident Manager creates an incident, the system captures the Amazon Resource Name (ARN) of the corresponding incident record and enters it for this parameter in the runbook.  
This value can only be assigned to parameters of type `String`. If assigned to a parameter of any other type, the runbook fails to run.

`Involved resources`  
When Incident Manager creates an incident, the system captures the ARNs of the resources involved in the incident. These resource ARNs are then assigned to this parameter in the runbook.

### About associated resources
<a name="runbooks-parameters-involved-resources"></a>

Incident Manager can populate runbook parameter values with the ARNs of AWS resources specified in CloudWatch alarms, EventBridge events, and manually created incidents. This section describes the different types of resources for which Incident Manager can capture ARNs when populating this parameter.

**CloudWatch alarms**  
When an incident is created from a CloudWatch alarm action, Incident Manager automatically extracts the following types of resources from the associated metrics. It then populates the chosen parameters with the following involved resources:


****  

| AWS service | Resource type | 
| --- | --- | 
| Amazon DynamoDB | Global secondary indexes<br />Streams<br />Tables | 
| Amazon EC2 | Images<br />Instances | 
| AWS Lambda | Function aliases<br />Function versions<br />Functions | 
| Amazon Relational Database Service (Amazon RDS) | Clusters<br />Database instances | 
| Amazon Simple Storage Service (Amazon S3) | Buckets | 

**EventBridge rules**  
When the system creates an incident from an EventBridge event, Incident Manager populates the chosen parameters with the `Resources` property in the event. For more information, see [Amazon EventBridge events](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-events.html) in the *Amazon EventBridge User Guide*. 

**Manually created incidents**  
When you create an incident by using the [StartIncident](https://docs.aws.amazon.com/incident-manager/latest/APIReference/API_StartIncident.html) API action, Incident Manager populates the chosen parameters by using information in the API call. Specifically, it populates parameters by using items of the type `INVOLVED_RESOURCE` that are passed in the `relatedItems` parameter.

**Note**  
The `INVOLVED_RESOURCES` value can only be assigned to parameters of type `StringList`. If assigned to a parameter of any other type, the runbook fails to run.

## Define a runbook
<a name="runbook-create"></a>

When creating a runbook, you can follow the steps provided here, or you can follow the more detailed guide provided in the [Working with runbooks](https://docs.aws.amazon.com/systems-manager/latest/userguide/automation-documents.html) section in the *Systems Manager User Guide*. If you're creating a multi-account, multi-Region runbook, see [Running automations in multiple AWS Regions and accounts](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation-multiple-accounts-and-regions.html) in the *Systems Manager User Guide*. 

**Define a runbook**

1. Open the Systems Manager console at [https://console.aws.amazon.com/systems-manager/](https://console.aws.amazon.com/systems-manager/). 

1. In the navigation pane, choose **Documents**.

1. Choose **Create automation**.

1. Enter a unique and identifiable runbook name.

1. Enter a description of the runbook.

1. Provide an IAM role for the automation document to assume. This allows the runbook to run commands automatically. For more information, see [Configuring a service role access for Automation workflows](https://docs.aws.amazon.com/systems-manager/latest/userguide/automation-setup.html#automation-setup-configure-role).

1. (Optional) Add any input parameters that the runbook starts with. You can use dynamic or static parameters when starting a runbook. Dynamic parameters use values from the incident that the runbook is started in. Static parameters use the value you provide.

1. (Optional) Add a **Target** type.

1. (Optional) Add tags.

1. Fill in the steps that the runbook will take when it runs. Each step requires:
   + A name.
   + A description of the purpose of the step.
   + The action to run during the step. Runbooks use the **Pause** action type to describe a manual step.
   + (Optional) Command properties.

1. After adding all required runbook steps, choose **Create Automation**.

To enable cross-account functionality, share the runbook in your management account with all application accounts that use the runbook during an incident. 

**Share a runbook**

1. Open the Systems Manager console at [https://console.aws.amazon.com/systems-manager/](https://console.aws.amazon.com/systems-manager/).

1. In the navigation pane, choose **Documents**.

1. In the documents list, choose the document you want to share and then choose **View details**. On the **Permissions** tab, verify that you're the document owner. Only a document owner can share a document.

1. Choose **Edit**.

1. To share the command publicly, choose **Public** and then choose **Save**. To share the command privately, choose **Private**, enter the AWS account ID, choose **Add permission**, and then choose **Save**. 

## Incident Manager runbook template
<a name="runbooks-template"></a>

Incident Manager provides the following runbook template to help your team start authoring runbooks in Systems Manager automation. You can use this template as is, or edit it to include details specific to your application and resources. 

**Find the Incident Manager runbook template**

1. Open the Systems Manager console at [https://console.aws.amazon.com/systems-manager/](https://console.aws.amazon.com/systems-manager/).

1. In the navigation pane, choose **Documents**.

1. In the **Documents** area, enter **AWSIncidents-** in search field to display all Incident Manager runbooks.
**Tip**  
Enter **AWSIncidents-** as free text instead of using the **Document name prefix** filter option.

**Using a template**

1. Open the Systems Manager console at [https://console.aws.amazon.com/systems-manager/](https://console.aws.amazon.com/systems-manager/).

1. In the navigation pane, choose **Documents**.

1. Choose the template you want to update from the documents list.

1. Choose the **Content** tab, and then copy the content of the document.

1. In the navigation pane, choose **Documents**.

1. Choose **Create automation**.

1. Enter a unique and identifiable name.

1. Choose the **Editor** tab. 

1. Choose **Edit**.

1. Paste or enter the copied details in the **Document editor** area. 

1. Choose **Create automation**.

### `AWSIncidents-CriticalIncidentRunbookTemplate`
<a name="runbooks-template-critical"></a>

The `AWSIncidents-CriticalIncidentRunbookTemplate` is a template that provides the Incident Manager incident lifecycle in manual steps. These steps are generic enough to use in most applications, but detailed enough for responders to get started with incident resolution. 
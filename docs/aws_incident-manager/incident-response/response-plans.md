

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Creating and configuring response plans in Incident Manager
<a name="response-plans"></a>

Response plans let you plan for how to respond to an incident that impacts your users. A response plan works as a template that includes information about who to engage, the expected severity of the event, automatic runbooks to initiate, and metrics to monitor. 

**Best practices**  
You can reduce the impact on incidents on your teams when you plan for incidents ahead of time. Teams should consider the following best practices when you design a response plan.
+ **Streamlined engagement** – Identify the most appropriate team for an incident. If you engage too wide a distribution list, or if you engage the wrong teams, you can cause confusion and waste responder time during an incident. 
+ **Reliable escalation** – For your engagements in a response plan, we recommend selecting an engagement plan instead of contacts or on-call schedules. The engagement plan should specify the individual contacts or on-call schedules (which contain multiple rotating contacts) to engage during incidents. Because responders specified in your engagement plan can be unreachable at times, you should configure backup responders in your response plan to cover these scenarios. With backup contacts, if the primary and secondary contacts are unavailable or there are other unplanned gaps in coverage, Incident Manager still notifies a contact about the incident.
+ **Runbooks** – Use runbooks to provide repeatable, understandable steps that reduce the stress a responder experiences during an incident.
+ **Collaboration** – Use chat channels to streamline communication during incidents. Chat channels help responders stay up to date with information. They can also share information with other responders through these channels. 

## Creating a response plan
<a name="response-plans-create"></a>

Use the following procedure to create a response plan and automate incident response.

**To create a response plan**

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home), and in the navigation pane, choose **Response plans**.

1. Choose **Create response plan**.

1. For **Name**, enter a unique and identifiable response plan name to use in the Amazon Resource Name (ARN) for the response plan.

1. (Optional) For **Display name**, enter a more human readable name to help identify the response plan when you create incidents.

1. Continue by [specifying default values for incident records](#incident-defaults).

### Specifying incident default values
<a name="incident-defaults"></a>

To help you manage incidents more effectively, you can specify default values. Incident Manager applies these values to all incidents that are associated with a response plan. 

**To specify incident default values**

1. For **Title**, enter a title for this incident to help you identify it on the Incident Manager home page.

1. For **Impact**, choose an impact level to indicate the potential scope of an incidents created from this response plan, such as **Critical** or **Low**. For information about impact ratings in Incident Manager, see [Triage](incident-lifecycle.md#triage).

1. (Optional) For **Summary**, enter a brief summary the type of incidents created from this response plan.

1. (Optional) For **Dedupe string**, enter a dedupe string. Incident Manager uses this string to prevent the same root cause from creating multiple incidents in the same account.

   A deduplication string is a term or phrase the system uses to check for duplicate incidents. If you specify a deduplication string, Incident Manager searches for open incidents that contain the same string in the `dedupeString` field when it creates the incident. If a duplicate is detected, Incident Manager deduplicates the newer incident into the existing incident.
**Note**  
By default, Incident Manager automatically deduplicates multiple incidents created by the same Amazon CloudWatch alarm or Amazon EventBridge event. You don't have to enter your own deduplication string to prevent duplication for these resource types.

1. (Optional) Under **Incident Tags**, add tag keys and values to assign to incidents created from this response plan. 

   You must have the `TagResource` permission for the incident record resource to set incident tags within the response plan.

1. Continue by [specifying an optional chat channel](#chat-channel) for resolvers to communicate with one another about incidents.

### (Optional) Specifying an incident response chat channel
<a name="chat-channel"></a>

When you include a chat channel in a response plan, responders receive incident updates through the channel. They can interact with the incident directly from the chat channel by using chat commands.

Using Amazon Q Developer in chat applications, you can create a channel for Slack, for Microsoft Teams, or for Amazon Chime to use in your response plans. For information about creating a chat channel in Amazon Q Developer in chat applications, see the [https://docs.aws.amazon.com/chatbot/latest/adminguide/](https://docs.aws.amazon.com/chatbot/latest/adminguide/).

**Important**  
Incident Manager must have permissions to publish to a chat channel's Amazon Simple Notification Service (Amazon SNS) topic. Without permissions to publish to that SNS topic, you can't add it to the response plan. Incident Manager publishes a test notification to the SNS topic to verify permissions.

For more information about chat channels, see [Creating and integrating chat channels for responders in Incident Manager](chat.md). 

**To specify an incident response chat channel**

1. For **Chat channel**, select an Amazon Q Developer in chat applications chat channel where responders can communicate during an incident. 
**Tip**  
To create a new chat channel in Amazon Q Developer in chat applications, choose **Configure new Chatbot client**.

1. For **Chat channel SNS topics**, choose additional SNS topics to publish to during the incident. Adding SNS topics in multiple AWS Regions increases redundancy in case a Region is down at the time of the incident.

1. Continue by [selecting the contacts, on-call schedules, and escalation plans](#engagements) to be engaged during an incident.

### (Optional) Select resources to engage in incident response
<a name="engagements"></a>

It's important to identify the most appropriate responders when an incident occurs. As a best practice, we recommend that you do the following:

1. Add contacts and on-call schedules as the escalation channels in an escalation plan.
**Note**  
Currently, the ability to add a contact that’s shared from another account to a response plan is not supported.

1. Choose an escalation plan as the engagement in a response plan.

For more information about contacts and escalation plans, see [Creating and configuring contacts in Incident Manager](contacts.md) and [Creating an escalation plan for responder engagement in Incident Manager](escalation.md).

**To select resources to engage in incident response**

1. For **Engagements**, choose any number of escalation plans, on-call schedules, and individual contacts.

1. Continue by optionally [specifying a runbook to run](#runbook) as part of your incident mitigation.

### (Optional) Specifying a runbook for incident mitigation
<a name="runbook"></a>

You can use runbooks from [AWS Systems Manager Automation](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation.html), a tool in AWS Systems Manager, to automate common application and infrastructure tasks in your AWS Cloud environment.

Each runbook defines an *runbook workflow*. A runbook workflow includes the actions that Systems Manager performs on your managed nodes or other AWS resource types. In Incident Manager, a runbook drives incident response and mitigation.

For more information about using runbooks in response plans, [Integrating Systems Manager Automation runbooks in Incident Manager for incident remediation](runbooks.md).

To specify a runbook for incident mitigation:

1. For **Runbook**, do one of the following:
   + Choose **Clone runbook from template** to make a copy of the default Incident Manager runbook. For **Runbook name**, enter a descriptive name for the new runbook. 
   + Choose **Select existing runbook**. Select the **Owner**, **Runbook**, and **Version** to use. 
**Tip**  
To create a runbook from scratch, choose **Configure new runbook**.  
For information about creating runbooks, see [Integrating Systems Manager Automation runbooks in Incident Manager for incident remediation](runbooks.md).

1. In the **Parameters** area, supply any parameters requested for the runbook you selected.

   The available parameters are those specified by the runbook. One runbook might require different parameters than another. Some parameters might be required and others optional.

   In many cases, you can choose to manually enter a static value for a parameter, such as a list of Amazon EC2 instance IDs. You can also let Incident Manager provide the parameter values that were dynamically generated by an incident.

1. (Optional) For **AutomationAssumeRole**, specify the AWS Identity and Access Management (IAM) role to use. This role must have the permissions needed to run the individual commands specified within the runbook. 
**Note**  
If no `AssumeRole` is specified, Incident Manager attempts to use the Runbook service role to run the individual commands specified within the runbook.

   Choose from the following:
   + **Enter ARN value** – Manually enter the Amazon Resource Name (ARN) of an AssumeRole, in the format `arn:aws:iam::{{account-id}}:role/{{assume-role-name}}`. For example, **arn:aws:iam::123456789012:role/MyAssumeRole**.
   + **Use existing service role** – Choose a role with the required permissions from a list of existing roles in your account.
   + **Create new service role** – Choose from among AWS managed policies to attach to your AssumeRole. After selecting this option, for **AWS managed policies**, choose one or more policies from the list.

     You can accept the suggested default name for the new role, or enter a name that you choose.
**Note**  
This new Runbook service role is associated with the specific runbook that you selected. It can't be used with different runbooks. This is because the Resource section of the policy won't support other runbooks.

1. For **Runbook service role**, specify the IAM role to use to provide the permissions needed to access and start the workflow for the runbook itself.

   At minimum, the role must allow the `ssm:StartAutomationExecution` action for your specific runbook. For the runbook to work across accounts, the role must also allow the `sts:AssumeRole` action for the `AWS-SystemsManager-AutomationExecutionRole` role that you created during [Managing incidents across AWS accounts and Regions in Incident Manager](incident-manager-cross-account-cross-region.md). 

   Choose from the following:
   + **Create new service role** – Incident Manager creates a Runbook service role for you that includes the minimum required permissions to start the runbook workflow.

     For **Role name**, you can accept the suggested default name, or enter a name that you choose. We recommend using the suggested name or keeping the name of the runbook in the name. This is because the new AssumeRole is associated with the specific runbook you selected and might not include the permissions required for other runbooks.
   + **Use existing service role** – An IAM role that you or Incident Manager created previously grants the needed permissions.

     For **Role name**, select the name of the existing role to use.

1. Expand **Additional options** and choose one of the following to specify the AWS account where the runbook workflow should run.
   + **Response plan owner's account** – Start the runbook workflow in the AWS account that created it.
   + **Impacted account** – Start the runbook workflow in the account that began or reported the incident.

     Choose **Impacted account** when you use Incident Manager for cross-account scenarios and the runbook needs to access resources in the impacted account to remediate them.

      

1. Continue by optionally [integrating a PagerDuty service into the response plan](#integrations).

### (Optional) Integrating a PagerDuty service into the response plan
<a name="integrations"></a>

**To integrate a PagerDuty service into the response plan**

When you integrate Incident Manager with PagerDuty, PagerDuty creates a corresponding incident whenever Incident Manager creates an incident. The incident in PagerDuty uses the paging workflow and escalation policies that you defined there in addition to those in Incident Manager. PagerDuty attaches timeline events from Incident Manager as notes on your incident.

1. Expand **Third-party integrations**, then choose the **Enable PagerDuty integration** check box.

1. For **Select secret**, select the secret in AWS Secrets Manager where you store the credentials to access your PagerDuty account.

   For information about storing your PagerDuty credentials in a Secrets Manager secret, see [Storing PagerDuty access credentials in an AWS Secrets Manager secret](integrations-pagerduty-secret.md).

1. For **PagerDuty service**, select the service from your PagerDuty account where you want to create the PagerDuty incident.

1. Continue by [adding optional tags and creating the response plan](#tags).

### Adding tags and creating the response plan
<a name="tags"></a>

**To add tags and create the response plan**

1. (Optional) In the **Tags** area, apply one or more tag key name/value pairs to the response plan.

   Tags are optional metadata that you assign to a resource. With tags, you can categorize a resource in different ways, such as by purpose, owner, or environment. For example, you might want to tag a response plan to identify the type of incident it is meant to mitigation, the types of escalation channels it contains, or the escalation plan that will be associated with it. For more information about tagging Incident Manager resources, see [Tagging resources in Incident Manager](tagging.md).

1. Choose **Create response plan**.

    
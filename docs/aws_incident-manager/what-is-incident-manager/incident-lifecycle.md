

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Incident lifecycle in Incident Manager
<a name="incident-lifecycle"></a>

AWS Systems Manager Incident Manager provides a step-by-step framework based on best practices to identify and react to incidents, such as service outages or security threats. The primary focus of Incident Manager is to help restore affected services or applications to normal as quickly as possible through a complete incident lifecycle management solution. 

As depicted in the following illustration, Incident Manager provides tools and best practices for every phase of the incident lifecycle:
+ [Alerting and engagement](#alerting-engagement)
+ [Triage](#triage)
+ [Investigation and mitigation](#investigation-mitigation)
+ [Post-incident analysis](#lifecycle-post-incident-analysis)

![The incident lifecycle includes alerting, engagement, triage, investigation, and analysis.](http://docs.aws.amazon.com/incident-manager/latest/userguide/images/incident-lifecycle.png)


## Alerting and engagement
<a name="alerting-engagement"></a>

The alerting and engagement phase of the incident lifecycle focuses on bringing awareness to incidents within your applications and services. This phase begins before an incident is ever detected and requires a deep understanding of your applications. You can use [Amazon CloudWatch metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/working_with_metrics.html) to monitor data about the performance of your applications, or use [Amazon EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/) to aggregate alerts from different sources, applications and services. After you've set up monitoring for your applications, you can begin alerting on metrics that stray outside the historical norm. To learn more about monitoring best practices, see [Monitoring](incident-response.md#incident-response-monitoring).

To support responders' incident diagnosis, you can enable the Findings feature in Incident Manager. Findings are information about AWS CodeDeploy deployments and AWS CloudFormation stack updates that occurred around the time of an incident. Having this information reduces the time needed to evaluate potential causes, which can reduce the mean time to recover (MTTR) from an incident.

Now that you are monitoring for incidents in your applications, you can define an incident *response plan* to use during an incident. To learn more about creating response plans, see [Creating and configuring response plans in Incident Manager](response-plans.md). Amazon EventBridge events or CloudWatch Alarms can automatically create an incident using with response plans as the template. To learn more about incident creation, see [Creating incidents automatically or manually in Incident Manager](incident-creation.md).

Response plans launch related *escalation plans* and *engagement plans* to bring first responders into the incident. For more information about setting up escalation plans, see [Create an escalation plan](escalation.md#escalation-create). Simultaneously, Amazon Q Developer in chat applications notifies responders using a *chat channel* directing them to the incident detail page. Using the chat channel and *incident details*, the team can communicate and triage an incident. For more information about setting up chat channels in Incident Manager, see [Task 2: Create a chat channel in Amazon Q Developer in chat applications](chat.md#chat-create).

## Triage
<a name="triage"></a>

Triage is when first responders attempt to determine the impact to customers. The incident details view in the Incident Manager console provides the responders with timelines and metrics to help them assess the incident. Assessing the impact of an incident also lays the groundwork for response time, resolution, and communication for the incident. Responders prioritize incidents by using impact ratings from 1 (Critical) to 5 (No Impact).

Your organization can define the exact scope of each impact rating however you choose. The following table provides examples of how each impact level might typically be defined.


| Impact code | Impact name | Sample defined scope | 
| --- | --- | --- | 
| 1 | Critical | Full application failure that impacts most customers. | 
| 2 | High | Full application failure that impacts a subset of customers. | 
| 3 | Medium | Partial application failure that is customer-impacting. | 
| 4 | Low | Intermittent failures that have limited impact on customers. | 
| 5 | No Impact | Customers aren't currently impacted but urgent action is needed to avoid impact. | 

## Investigation and mitigation
<a name="investigation-mitigation"></a>

The *incident* details view provides your team with runbooks, timelines, and metrics. To see how you can work with an incident, see the [Viewing incident details in the console](tracking.md#tracking-details).

*Runbooks* commonly provide investigation steps and can automatically pull data or attempt commonly used solutions. Runbooks also provide clear, repeatable steps that your team has found to be useful in mitigating incidents. The runbook tab focuses on the current runbook step and shows past and future steps.

Incident Manager integrates with Systems Manager Automation to build runbooks. Use runbooks to do any of the following:
+ Manage instances and AWS resources
+ Automatically run scripts
+ Manage CloudFormation resources

For more information about the supported action types, see [Systems Manager Automation actions reference](https://docs.aws.amazon.com/systems-manager/latest/userguide/automation-actions.html) in the *AWS Systems Manager User Guide*.

The **Timeline** tab shows what actions have been taken. The timeline records each with a timestamp and automatically created details. To add custom events to the timeline, see the [Timeline](tracking.md#tracking-details-timeline) section in the *Incident details* page of this user guide.

The **Diagnosis** tab shows automatically populated metrics and manually added metrics. This view provides valuable information into the activities of your application during an incident.

The **Engagements** tab allows you to add additional contacts to the incident and helps provide the resources for the engaged contact to get up to speed quickly once involved in the incident. Contacts are engaged through defined escalation plans or personal engagement plans.

Using a *chat channel*, you can directly interact with your incident and other responders on your team. Using Amazon Q Developer in chat applications, you can configure chat channels in. Slack, Microsoft Teams, and Amazon Chime. In Slack and Microsoft Teams channels, responders can interact with incidents directly from the chat channel using a number of `ssm-incidents` commands. For more information, see [Interacting through the chat channel](chat.md#chat-interact).

## Post-incident analysis
<a name="lifecycle-post-incident-analysis"></a>

Incident Manager provides a framework for reflecting on an incident, taking steps needed to prevent the incident from occurring again in the future, and to improve incident response activities overall. Improvements can include:
+ Changes to the applications involved in an incident. Your team can use this time to improve the system and make it more fault tolerant.
+ Changes to an incident response plan. Take the time to incorporate learned lessons.
+ Changes to runbooks. Your team can dive deep into steps needed for resolution and the steps that you can automate. 
+ Changes to alerting. After an incident, your team might have noticed critical points in the metrics you can use to alert the team sooner about an incident. 

Incident Manager facilitates these potential improvements by using a set of post-incident analysis questions and action items alongside the incident timeline. To learn more about improvement through analysis, see [Performing a post-incident analysis in Incident Manager](analysis.md).
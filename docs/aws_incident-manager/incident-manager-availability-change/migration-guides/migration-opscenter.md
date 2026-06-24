

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Migrating to AWS Systems Manager OpsCenter
<a name="migration-opscenter"></a>

This guide helps you understand key differences between Incident Manager and OpsCenter to decide if OpsCenter fits your operational needs and provides ways to migrate to OpsCenter from AWS Systems Manager Incident Manager.

[AWS Systems Manager OpsCenter](https://docs.aws.amazon.com/systems-manager/latest/userguide/OpsCenter.html), a capability of AWS Systems Manager, provides a central location where operations engineers and IT professionals can view, investigate, and resolve operational work items (OpsItems) related to AWS resources. OpsCenter is designed to reduce mean time to resolution (MTTR) for issues impacting AWS resources. OpsCenter aggregates and standardizes OpsItems across services while providing contextual investigation data about each OpsItem, related OpsItems, and related resources. OpsCenter integrates with Systems Manager Automation, allowing you to use Automation runbooks to investigate and resolve issues. You can view automatically-generated summary reports about OpsItems by status and source. You can also use [OpsCenter's cross-account](https://docs.aws.amazon.com/systems-manager/latest/userguide/OpsCenter-setting-up-cross-account.html) capability to centrally manage OpsItems across accounts.

**Note**  
There are charges associated with the OpsCenter use. Please refer to the [AWS Systems Manager pricing page](https://aws.amazon.com/systems-manager/pricing/) for more details.

Similar to Incident Manager, OpsCenter has integrations with Amazon CloudWatch and Amazon EventBridge. This means you can configure these services to automatically create an OpsItem in OpsCenter when a CloudWatch alarm enters the `ALARM` state or when EventBridge processes an event from any AWS service that publishes events. Configuring CloudWatch alarms and EventBridge events to automatically create OpsItems allows you to quickly diagnose and remediate issues with AWS resources from a single console.

## Understanding the differences
<a name="opscenter-differences"></a>

AWS Systems Manager Incident Manager provides incident response capabilities including automated response plans, responder engagement and escalation, on-call rotation management, runbook automation, chat-ops integration (Slack, Microsoft Teams, Amazon Chime), and post-incident analysis. These features help organizations coordinate and resolve critical, time-sensitive incidents affecting AWS-hosted applications.

In contrast, AWS Systems Manager OpsCenter focuses on managing operational work items (OpsItems) for day-to-day operational issues such as security alerts, performance degradation, resource failures, health notifications, and state changes. OpsCenter integrates with AWS resources through Amazon CloudWatch and Amazon EventBridge, enabling automated OpsItem creation and remediation using Systems Manager Automation runbooks. OpsCenter supports cross-account management of OpsItems within a region, allowing operations teams to view, investigate, and resolve issues across multiple AWS accounts. However, OpsCenter does not include paging or on-call rotation capabilities.

The key differences between these two AWS services lie in their focus and scope. Incident Manager is designed for critical, time-sensitive incident response, while OpsCenter is oriented towards the management of broader operational tasks and work items.

The following table compares key capabilities between Incident Manager and OpsCenter. Use this comparison to decide if OpsCenter fits your operational needs.


| Feature/Capability | AWS Systems Manager Incident Manager | AWS Systems Manager OpsCenter | 
| --- | --- | --- | 
| Primary Purpose | Critical, time-sensitive incident response and coordination | Day-to-day operational work item management | 
| Use Cases | Application-impacting incidents; Security breaches; Service outages; Critical system failures | Security alerts; Performance degradation; Resource failures; Health notifications; State changes | 
| Automated Paging | Yes - Built-in paging and responder engagement | No - Requires third-party integration (PagerDuty, ServiceNow, Jira) | 
| On-Call Rotation Management | Yes - Native on-call schedules and rotation | No - Not supported | 
| Escalation Policies | Yes - Automated escalation chains | No - Manual escalation required | 
| Chat-Ops Integration | Yes - Slack, Microsoft Teams, Amazon Chime | Limited - Manual integration required | 
| Runbook Automation | Yes - Automated execution via response plans | Yes - Manual execution of Systems Manager Automation runbooks | 
| Cross-Account Management | Yes - Cross-account incident sharing | Yes - Cross-account OpsItem management within a region | 

## Migration options
<a name="migration-options"></a>

If you have existing CloudWatch alarms and EventBridge rules integrated with Incident Manager, you'll need to update them to integrate with OpsCenter. You can migrate using one of the following approaches:

Automated migration using runbooks  
Use [Systems Manager Automation](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-automation.html) runbooks to automatically migrate your CloudWatch alarms and EventBridge rules from Incident Manager to OpsCenter. This approach includes backup, configurable approval workflows, and detailed logging. You can choose to require manual approval before migration or skip the approval step for automated large-scale migrations. For step-by-step instructions, see [Using migration runbooks for OpsCenter](migration-opscenter-runbooks.md).

Manual integration  
Manually configure your CloudWatch alarms and EventBridge rules to integrate with OpsCenter. For instructions, see [Configuring CloudWatch alarms to create OpsItems](https://docs.aws.amazon.com/systems-manager/latest/userguide/OpsCenter-create-OpsItems-from-CloudWatch-Alarms.html) and [Configuring EventBridge to create OpsItems](https://docs.aws.amazon.com/systems-manager/latest/userguide/OpsCenter-automatically-create-OpsItems-2.html) in the Systems Manager User Guide.

## Related resources
<a name="related-resources-opscenter"></a>
+ [AWS Systems Manager OpsCenter User Guide](https://docs.aws.amazon.com/systems-manager/latest/userguide/OpsCenter.html)
+ [Exporting Incident Manager data](export-data.md)
+ [Cleaning up Incident Manager Resources](migration-cleanup.md)
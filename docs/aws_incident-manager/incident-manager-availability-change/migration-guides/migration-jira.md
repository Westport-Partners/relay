

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Migrating to Jira Service Management
<a name="migration-jira"></a>

[Jira Service Management (JSM)](https://www.atlassian.com/software/jira/service-management/features/itsm#incident-management) is an IT service management (ITSM) solution that helps teams receive, track, manage, and resolve employee and customer requests through multiple channels including email, chat, help centers, and widgets. Built on the Jira platform, Jira Service Management enables teams across an organization - from development to IT to HR - to intake requests, respond to alerts and incidents, deploy changes, track assets, surface knowledge, and automate workflows. Jira Service Management includes incident management capabilities such as on-call scheduling, alerting, major incident management, change management and blameless post mortem (PIR) features designed for DevOps workflows, leveraging existing CI/CD pipelines and automation to reduce manual effort.

Jira Service Management integrates with Amazon CloudWatch and Amazon EventBridge, allowing you to automatically create Jira Service Management alerts when CloudWatch alarms enter the `ALARM` state or when EventBridge processes events from any AWS service that publishes events. Configuring CloudWatch alarms and EventBridge events to automatically create Jira Service Management alerts enables you to quickly diagnose and remediate issues with AWS resources from a single platform. Jira Service Management acts as a dispatcher, notifying the right people through multiple channels (email, SMS, phone calls, mobile push) based on on-call schedules and escalation policies.

If you have existing CloudWatch Alarms and EventBridge Rules integrated with AWS Systems Manager Incident Manager, we recommend you update those integrations to use Jira Service Management instead. The official Atlassian documentation provides detailed instructions for [Integrating Jira Service Management with CloudWatch](https://support.atlassian.com/jira-service-management-cloud/docs/integrate-with-amazon-cloudwatch/) and [Integrating Jira Service Management with EventBridge](https://support.atlassian.com/jira-service-management-cloud/docs/integrate-with-amazon-eventbridge/).

Along with automated alert creation, Jira Service Management offers a range of features to streamline incident management, such as on-call scheduling, escalation policies, and automation rules. Customers can refer to the following Atlassian documentation for details on configuring these capabilities:
+ [Discover Alerts & On-call](https://support.atlassian.com/jira-service-management-cloud/docs/discover-alerting-and-on-call/)
+ [Create On-Call Schedules](https://support.atlassian.com/jira-service-management-cloud/docs/create-an-on-call-schedule/)
+ [Create Escalation Policies](https://support.atlassian.com/jira-service-management-cloud/docs/create-edit-delete-an-escalation-policy/)
+ [Set Up Teams and People](https://support.atlassian.com/platform-experiences/docs/start-an-atlassian-team/)
+ [Set Up Contact Methods](https://support.atlassian.com/jira-service-management-cloud/docs/add-contact-methods/)
+ [Configure Notification Rules](https://support.atlassian.com/jira-service-management-cloud/docs/add-notification-rules/)
+ [Set up SMS and voice notifications](https://support.atlassian.com/jira-service-management-cloud/docs/set-up-sms-and-voice-notifications/)
+ [Set Up Automation Rules](https://www.atlassian.com/software/jira/service-management/product-guide/tips-and-tricks/automation#overview)
+ [Set Up & manage incident stakeholders](https://support.atlassian.com/jira-service-management-cloud/docs/how-can-i-add-and-manage-internal-stakeholders/)

For additional support, you can contact your Technical Account Manager or [an Atlassian sales representative](https://www.atlassian.com/enterprise/contact) for more information.
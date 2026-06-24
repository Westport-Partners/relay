

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Migrating to PagerDuty
<a name="migration-pagerduty"></a>

[PagerDuty](https://support.pagerduty.com/main/docs/introduction) is an incident management platform that helps organizations detect, respond to, and even prevent incidents. Like Incident Manager, PagerDuty provides a central location where operations teams tackle critical work related to AWS resources, reducing customer impact.

PagerDuty integrates with Amazon CloudWatch and Amazon EventBridge, allowing you to automatically create PagerDuty incidents when CloudWatch alarms enter the `ALARM` state or when EventBridge processes events from any AWS service that publishes events. By configuring CloudWatch alarms and EventBridge events to automatically create PagerDuty incidents, you can quickly diagnose and remediate AWS resource issues from a single platform.

If you have existing CloudWatch Alarms and EventBridge Rules integrated with AWS Systems Manager Incident Manager, we recommend you update those integrations to use PagerDuty instead. The official PagerDuty documentation provides detailed instructions for [Integrating PagerDuty with CloudWatch](https://support.pagerduty.com/main/docs/amazon-cloudwatch-integration-guide) and [Integrating PagerDuty with EventBridge](https://support.pagerduty.com/main/docs/amazon-eventbridge-integration-guide).

Along with automated incident creation, PagerDuty offers a range of features to improve incident management, such as on-call scheduling, escalation policies, and over 700\+ out-of-box platform integrations. You can also customize notification rules, configure chat surfaces, and leverage AI and automation within the PagerDuty platform to accelerate incident resolution.
+ [Manage Users](https://support.pagerduty.com/main/docs/manage-users)
+ [Create Teams](https://support.pagerduty.com/main/docs/teams)
+ [Set Up Contact Methods](https://support.pagerduty.com/main/docs/contact-information)
+ [Configure Notification Rules](https://support.pagerduty.com/main/docs/notification-rules)
+ [Set Up an On-Call Rotation](https://support.pagerduty.com/main/docs/schedule-basics)
+ [Create Escalation Policies](https://support.pagerduty.com/main/docs/escalation-policies)
+ [Configure Slack Integration](https://support.pagerduty.com/main/docs/slack-integration-guide)
+ [Set Up Automation Actions](https://support.pagerduty.com/main/docs/automation-actions)

For additional support, you can contact your Technical Account Manager or [AWS-IM-help@pagerduty.com](mailto:AWS-IM-help@pagerduty.com) for more information.
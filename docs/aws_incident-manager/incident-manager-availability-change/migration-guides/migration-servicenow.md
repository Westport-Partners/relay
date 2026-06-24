

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Migrating to ServiceNow
<a name="migration-servicenow"></a>

ServiceNow [Incident Management](https://www.servicenow.com/docs/bundle/zurich-it-service-management/page/product/incident-management/concept/c_IncidentManagement.html) is a core ITSM module designed to restore normal service operations after unplanned interruptions while minimizing business impact. Like Incident Manager, ServiceNow Incident Management provides a structured, automated system to view, investigate, and resolve IT incidents, with features such as automated prioritization, and built-in escalation processes.

The ServiceNow Service Operations with Incident Management and Event management module integrates with Amazon CloudWatch, allowing you to automatically create ServiceNow events/alerts and incidents when CloudWatch alarms enter the `ALARM` state. Configuring CloudWatch alarms to automatically create ServiceNow incidents with webhook to AIOps event management enables you to quickly diagnose and remediate issues with AWS resources from a single platform.

If you have existing CloudWatch Alarms integrated with AWS Systems Manager Incident Manager, we recommend you update those integrations to use ServiceNow [Incident Management](https://www.servicenow.com/products/incident-management.html) and [AIOps event intelligence](https://www.servicenow.com/products/event-management.html) platform instead. The official ServiceNow documentation provides detailed instructions for [integrating ServiceNow with Amazon CloudWatch](https://www.servicenow.com/docs/bundle/zurich-integrate-applications/page/administer/integrationhub-store-spokes/concept/amazon-cloudwatch.html).

Along with automated incident creation, ServiceNow Incident Management offers a range of features to improve incident management, such as incident communications management, on-call scheduling, escalation policies, and more. Customers can refer to the following ServiceNow documentation for details on configuring these capabilities:
+ [Incident Management Documentation](https://www.servicenow.com/docs/bundle/zurich-it-service-management/page/product/incident-management/concept/c_IncidentManagement.html)
+ [Service Reliability Management](https://www.servicenow.com/docs/bundle/zurich-it-operations-management/page/product/service-reliability/reference/sr-landing-page.html)
+ [Incident Communications Management and Contacts](https://www.servicenow.com/docs/bundle/zurich-it-service-management/page/product/incident-alert-management/concept/c_IncidentAlertContact.html)
+ [On-Call Schedules](https://www.servicenow.com/docs/bundle/zurich-it-service-management/page/administer/on-call-scheduling/concept/c_OnCallScheduling.html)
+ [Escalation process](https://www.servicenow.com/docs/bundle/zurich-it-service-management/page/administer/on-call-scheduling/concept/designing-escalation-process-oncall.html)

For additional support, you can contact your Technical Account Manager or a [ServiceNow sales representative](https://www.servicenow.com/be/contact-us/sales.html) for more information.
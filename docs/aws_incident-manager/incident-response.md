

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Preparing for incidents in Incident Manager
<a name="incident-response"></a>

Planning for an incident begins long before the incident lifecycle. As the following illustration shows, before starting to respond to incidents, you get prepared by setting up chat channels, creating escalation plans, specifying contacts, and determining the Automation runbooks to use in incident response. Then, use a response plan that specifies how monitoring occurs and whether responses are automated. After remediation is complete, you can analyze the incident and incident response to further refine your response plan for future incidents. 

![An Incident Manager workflow for preparing for, responding to, and learning from incidents.](http://docs.aws.amazon.com/incident-manager/latest/userguide/images/how-it-works.png)


**Topics**
+ [Monitoring](#incident-response-monitoring)
+ [Configuring replication sets and Findings in Incident Manager](general-settings.md)
+ [Creating and configuring contacts in Incident Manager](contacts.md)
+ [Managing responder rotations with on-call schedules in Incident Manager](incident-manager-on-call-schedule.md)
+ [Creating an escalation plan for responder engagement in Incident Manager](escalation.md)
+ [Creating and integrating chat channels for responders in Incident Manager](chat.md)
+ [Integrating Systems Manager Automation runbooks in Incident Manager for incident remediation](runbooks.md)
+ [Creating and configuring response plans in Incident Manager](response-plans.md)
+ [Identifying potential causes of incidents from other services as "findings" in Incident Manager](findings.md)

## Monitoring
<a name="incident-response-monitoring"></a>

Monitoring the health of your AWS hosted applications is key to ensuring application up time and performance. When determining monitoring solutions, consider the following: 
+ **Criticality of feature** – If the system were to fail, how critical would the impact to downstream users be.
+ **Commonality of failure** – How commonly does a system fail; systems that require frequent intervention should be closely monitored.
+ **Increased latency** – How much the time to complete a task has increased or decreased.
+ **Client-side versus server-side metrics** – If there is a discrepancy between related metrics on the client and server.
+ **Dependency failures** – Failures that your team can and should prepare for.

After creating response plans, you can use your monitoring solutions to automatically track incidents the moment they happen in your environment. For more information about incident tracking and creation, see [Viewing incident details in the Incident Manager console](tracking.md).

For more information about architecting secure, high-performing, resilient, and efficient infrastructure applications and workloads, see the [AWS Well-Architected](https://aws.amazon.com/architecture/well-architected/).
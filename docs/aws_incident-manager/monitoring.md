

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Monitoring in Incident Manager
<a name="monitoring"></a>

AWS Systems Manager Incident Manager integrates with the following services that offer monitoring and logging capabilities:

**CloudWatch metrics**  
Use CloudWatch metrics to retrieve statistics about data points for your AWS Systems Manager Incident Manager operations as an ordered set of time series data, known as *metrics*. You can use these metrics to verify that your system is performing as expected. For more information, see [Monitoring metrics in Incident Manager with Amazon CloudWatch](incident-manager-metrics.md).

**CloudTrail logs**  
Use AWS CloudTrail to capture detailed information about the calls made to AWS APIs. You can store these calls as log files in Amazon Simple Storage Service.. You can use these CloudTrail logs to determine such information as which call was made, the source IP address where the call came from, who made the call, and when the call was made. The CloudTrail logs contain information about the calls to API actions for Incident Manager. lFor more information, see [Logging AWS Systems Manager Incident Manager API calls using AWS CloudTrail](logging-using-cloudtrail.md).

**Trusted Advisor**  
AWS Trusted Advisor can help you monitor your AWS resources to improve performance, reliability, security, and cost effectiveness. Four Trusted Advisor checks are available to all users; more than 50 checks are available to users with a Business or Enterprise support plan. For Incident Manager, Trusted Advisor checks that a replication set’s configuration uses more than one AWS Region to support regional failover and response. For more information, see [AWS Trusted Advisor](https://docs.aws.amazon.com/awssupport/latest/user/trusted-advisor.html) in the *AWS Support User Guide*.
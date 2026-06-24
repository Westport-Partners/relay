

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Working with AWS Systems Manager Incident Manager and interface VPC endpoints (AWS PrivateLink)
<a name="vpc-interface-endpoints"></a>

You can establish a private connection between your VPC and AWS Systems Manager Incident Manager by creating an *interface VPC endpoint*. Interface endpoints are powered by AWS PrivateLink. With AWS PrivateLink, you can privately access Incident Manager API operations without an internet gateway, NAT device, VPN connection, or Direct Connect connection.. Instances in your VPC don't need public IP addresses to communicate with Incident Manager API operations. Traffic between your VPC and Incident Manager stays within the Amazon network. 

Each interface endpoint is represented by one or more [Elastic Network Interfaces](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html) in your subnets. 

For more information, see [Interface VPC endpoints (AWS PrivateLink)](https://docs.aws.amazon.com/vpc/latest/userguide/vpce-interface.html) in the *Amazon VPC User Guide*. 

## Considerations for Incident Manager VPC endpoints
<a name="vpc-endpoint-considerations"></a>

Before you set up an interface VPC endpoint for Incident Manager, ensure that you review [Interface endpoint properties and limitations](https://docs.aws.amazon.com/vpc/latest/privatelink/vpce-interface.html#vpce-interface-limitations) and [AWS PrivateLink quotas](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-limits-endpoints.html) in the *Amazon VPC User Guide*. 

Incident Manager supports making calls to all of its API actions from your VPC. To use all of Incident Manager, you must create two VPC endpoints: one for `ssm-incidents` and one for `ssm-contacts`.

## Creating an interface VPC endpoint for Incident Manager
<a name="vpc-endpoint-create"></a>

You can create a VPC endpoint for Incident Manager using either the Amazon VPC console or the AWS Command Line Interface (AWS CLI). For more information, see [Creating an interface endpoint](https://docs.aws.amazon.com/vpc/latest/userguide/vpce-interface.html#create-interface-endpoint) in the *Amazon VPC User Guide*.

Create a VPC endpoint for Incident Manager using supported service names for Incident Manager in your AWS Region. The following examples show the interface endpoint formats for IPv4 and dual-stack endpoints.

IPv4 endpoint formats   
+ `com.amazonaws.{{region}}.ssm-incidents` 
+ `com.amazonaws.{{region}}.ssm-contacts`

Dual-stack (IPv4 and IPv6) endpoint formats  
+ `aws.api.{{region}}.ssm-incidents` 
+ `aws.api.{{region}}.ssm-contacts`

For lists of supported endpoints for all Regions, see [AWS Systems Manager Incident Manager endpoints and quotas](https://docs.aws.amazon.com/general/latest/gr/incident-manager.html) in the *AWS General Reference Guide*.

If you enable private DNS for the interface endpoint, you can make API requests to Incident Manager using its default Regional DNS names in the format. The following examples show the default Regional DNS names format. 
+ `ssm-incidents.{{region}}.amazonaws.com`
+ `ssm-contacts.{{region}}.amazonaws.com`



For more information, see [Accessing a service through an interface endpoint](https://docs.aws.amazon.com/vpc/latest/userguide/vpce-interface.html#access-service-though-endpoint) in the *Amazon VPC User Guide*.

## Creating a VPC endpoint policy for Incident Manager
<a name="vpc-endpoint-policy"></a>

You can attach an endpoint policy to your VPC endpoint that controls access to Incident Manager. The policy specifies the following information:
+ The principal that can perform actions.
+ The actions that can be performed.
+ The resources on which these actions can be performed.

For more information, see [Controlling access to services with VPC endpoints](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-endpoints-access.html) in the *Amazon VPC User Guide*. 

**Example: VPC endpoint policy for Incident Manager actions**  
The following is an example of an endpoint policy for Incident Manager. When attached to an endpoint, this policy grants access to the listed Incident Manager actions for all principals on all resources.

```
{
   "Statement":[
      {
         "Principal":"*",
         "Effect":"Allow",
         "Action":[
            "{{ssm-contacts}}:{{ListContacts}}",
            "{{ssm-incidents}}:{{ListResponsePlans}}",
            "{{ssm-incidents}}:{{StartIncident}}"
         ],
         "Resource":"*"
      }
   ]
}
```
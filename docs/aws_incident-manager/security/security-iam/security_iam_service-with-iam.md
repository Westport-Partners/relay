

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# How AWS Systems Manager Incident Manager works with IAM
<a name="security_iam_service-with-iam"></a>

Before you use IAM to manage access to Incident Manager, learn what IAM features are available to use with Incident Manager.






**IAM features you can use with AWS Systems Manager Incident Manager**  

| IAM feature | Incident Manager support | 
| --- | --- | 
| [Identity-based policies](#security_iam_service-with-iam-id-based-policies) |  Yes | 
| [Resource-based policies](#security_iam_service-with-iam-resource-based-policies) |  Yes | 
| [Policy actions](#security_iam_service-with-iam-id-based-policies-actions) |  Yes | 
| [Policy resources](#security_iam_service-with-iam-id-based-policies-resources) |  Yes | 
| [Policy condition keys](#security_iam_service-with-iam-id-based-policies-conditionkeys) |  No  | 
| [ACLs](#security_iam_service-with-iam-acls) |  No  | 
| [ABAC (tags in policies)](#security_iam_service-with-iam-tags) |  No  | 
| [Temporary credentials](#security_iam_service-with-iam-roles-tempcreds) |  Yes | 
| [Principal permissions](#security_iam_service-with-iam-principal-permissions) |  Yes | 
| [Service roles](#security_iam_service-with-iam-roles-service) |  Yes | 
| [Service-linked roles](#security_iam_service-with-iam-roles-service-linked) |  Yes | 

To get a high-level view of how Incident Manager and other AWS services work with most IAM features, see [AWS services that work with IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_aws-services-that-work-with-iam.html) in the *IAM User Guide*.

Incident Manager doesn't support policies that deny access to resources shared using AWS RAM.

## Identity-based policies for Incident Manager
<a name="security_iam_service-with-iam-id-based-policies"></a>

**Supports identity-based policies:** Yes

Identity-based policies are JSON permissions policy documents that you can attach to an identity, such as an IAM user, group of users, or role. These policies control what actions users and roles can perform, on which resources, and under what conditions. To learn how to create an identity-based policy, see [Define custom IAM permissions with customer managed policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_create.html) in the *IAM User Guide*.

With IAM identity-based policies, you can specify allowed or denied actions and resources as well as the conditions under which actions are allowed or denied. To learn about all of the elements that you can use in a JSON policy, see [IAM JSON policy elements reference](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements.html) in the *IAM User Guide*.

### Identity-based policy examples for Incident Manager
<a name="security_iam_service-with-iam-id-based-policies-examples"></a>



To view examples of Incident Manager identity-based policies, see [Identity-based policy examples for AWS Systems Manager Incident Manager](security_iam_id-based-policy-examples.md).

## Resource-based policies within Incident Manager
<a name="security_iam_service-with-iam-resource-based-policies"></a>

**Supports resource-based policies:** Yes

Resource-based policies are JSON policy documents that you attach to a resource. Examples of resource-based policies are IAM *role trust policies* and Amazon S3 *bucket policies*. In services that support resource-based policies, service administrators can use them to control access to a specific resource. For the resource where the policy is attached, the policy defines what actions a specified principal can perform on that resource and under what conditions. You must [specify a principal](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html) in a resource-based policy. Principals can include accounts, users, roles, federated users, or AWS services.

To enable cross-account access, you can specify an entire account or IAM entities in another account as the principal in a resource-based policy. For more information, see [Cross account resource access in IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies-cross-account-resource-access.html) in the *IAM User Guide*.

The Incident Manager service supports only two types of resource-based policies called using either the AWS RAM console or the PutResourcePolicy action, which is attached to a response plan or contact. This policy defines which principals can perform actions on the response plans, contacts, escalation plans, and incidents. Incident Manager uses resource based policies to share resources across accounts.

Incident Manager doesn't support policies that deny access to resources shared using AWS RAM.

To learn how to attach a resource-based policy to a response plan or contact, see [Managing incidents across AWS accounts and Regions in Incident Manager](incident-manager-cross-account-cross-region.md).

### Resource-based policy examples within Incident Manager
<a name="security_iam_service-with-iam-resource-based-policies-examples"></a>



To view examples of Incident Manager resource-based policies, see [Resource-based policy examples for AWS Systems Manager Incident Manager](security_iam_resource-based-policy-examples.md).

## Policy actions for Incident Manager
<a name="security_iam_service-with-iam-id-based-policies-actions"></a>

**Supports policy actions:** Yes

Administrators can use AWS JSON policies to specify who has access to what. That is, which **principal** can perform **actions** on what **resources**, and under what **conditions**.

The `Action` element of a JSON policy describes the actions that you can use to allow or deny access in a policy. Include actions in a policy to grant permissions to perform the associated operation.



To see a list of Incident Manager actions, see [Actions defined by AWS Systems Manager Incident Manager](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssystemsmanagerincidentmanager.html#awssystemsmanagerincidentmanager-actions-as-permissions) in the *Service Authorization Reference*.

Policy actions in Incident Manager use the following prefixes before the action:

```
ssm-incidents
ssm-contacts
```

To specify multiple actions in a single statement, separate them with commas.

```
"Action": [
      "ssm-incidents:{{GetResponsePlan}}",
      "ssm-contacts:{{GetContact}}"
         ]
```





You can specify multiple actions using wildcards (\*). For example, to specify all actions that begin with the word `Get`, include the following action:

```
"Action": "ssm-incidents:Get*"
```

To view examples of Incident Manager identity-based policies, see [Identity-based policy examples for AWS Systems Manager Incident Manager](security_iam_id-based-policy-examples.md).

Incident Manager uses actions in two different namespaces, ssm-incidents and ssm-contacts. When creating policies for Incident Manager make sure to use the namespace correct for the action. SSM-Incidents is used for response plan and incident related action. SSM-Contacts is used for actions related to contacts and contact engagement. For example:
+ `ssm-contacts:GetContact`
+ `ssm-incidents:GetResponsePlan`

## Policy resources for Incident Manager
<a name="security_iam_service-with-iam-id-based-policies-resources"></a>

**Supports policy resources:** Yes

Administrators can use AWS JSON policies to specify who has access to what. That is, which **principal** can perform **actions** on what **resources**, and under what **conditions**.

The `Resource` JSON policy element specifies the object or objects to which the action applies. As a best practice, specify a resource using its [Amazon Resource Name (ARN)](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference-arns.html). For actions that don't support resource-level permissions, use a wildcard (\*) to indicate that the statement applies to all resources.

```
"Resource": "*"
```

To see a list of Incident Manager resource types and their ARNs, see [Resources defined by AWS Systems Manager Incident Manager](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssystemsmanagerincidentmanager.html#awssystemsmanagerincidentmanager-resources-for-iam-policies) in the *Service Authorization Reference*. To learn with which actions you can specify the ARN of each resource, see [Actions defined by AWS Systems Manager Incident Manager](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssystemsmanagerincidentmanager.html#awssystemsmanagerincidentmanager-actions-as-permissions).





To view examples of Incident Manager identity-based policies, see [Identity-based policy examples for AWS Systems Manager Incident Manager](security_iam_id-based-policy-examples.md).

Incident Manager resources are used to create incidents, collaborate in chat channels, resolve incidents, and engage responders. If a user has access to a response plan they have access to all incidents created from it. If a user has access to a contact or escalation plan they can engage the contact or contacts in the escalation plan.

## Policy condition keys for Incident Manager
<a name="security_iam_service-with-iam-id-based-policies-conditionkeys"></a>

**Supports service-specific policy condition keys:** No 

Administrators can use AWS JSON policies to specify who has access to what. That is, which **principal** can perform **actions** on what **resources**, and under what **conditions**.

The `Condition` element specifies when statements execute based on defined criteria. You can create conditional expressions that use [condition operators](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_condition_operators.html), such as equals or less than, to match the condition in the policy with values in the request. To see all AWS global condition keys, see [AWS global condition context keys](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html) in the *IAM User Guide*.

## Access control lists (ACLs) in Incident Manager
<a name="security_iam_service-with-iam-acls"></a>

**Supports ACLs:** No 

Access control lists (ACLs) control which principals (account members, users, or roles) have permissions to access a resource. ACLs are similar to resource-based policies, although they do not use the JSON policy document format.

## Attribute-based access control (ABAC) with Incident Manager
<a name="security_iam_service-with-iam-tags"></a>

**Supports ABAC (tags in policies):** No 

Attribute-based access control (ABAC) is an authorization strategy that defines permissions based on attributes called tags. You can attach tags to IAM entities and AWS resources, then design ABAC policies to allow operations when the principal's tag matches the tag on the resource.

To control access based on tags, you provide tag information in the [condition element](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_condition.html) of a policy using the `aws:ResourceTag/{{key-name}}`, `aws:RequestTag/{{key-name}}`, or `aws:TagKeys` condition keys.

If a service supports all three condition keys for every resource type, then the value is **Yes** for the service. If a service supports all three condition keys for only some resource types, then the value is **Partial**.

For more information about ABAC, see [Define permissions with ABAC authorization](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction_attribute-based-access-control.html) in the *IAM User Guide*. To view a tutorial with steps for setting up ABAC, see [Use attribute-based access control (ABAC)](https://docs.aws.amazon.com/IAM/latest/UserGuide/tutorial_attribute-based-access-control.html) in the *IAM User Guide*.

## Using temporary credentials with Incident Manager
<a name="security_iam_service-with-iam-roles-tempcreds"></a>

**Supports temporary credentials:** Yes

Temporary credentials provide short-term access to AWS resources and are automatically created when you use federation or switch roles. AWS recommends that you dynamically generate temporary credentials instead of using long-term access keys. For more information, see [Temporary security credentials in IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp.html) and [AWS services that work with IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_aws-services-that-work-with-iam.html) in the *IAM User Guide*.

## Cross-service principal permissions for Incident Manager
<a name="security_iam_service-with-iam-principal-permissions"></a>

**Supports forward access sessions (FAS):** Yes

 Forward access sessions (FAS) use the permissions of the principal calling an AWS service, combined with the requesting AWS service to make requests to downstream services. For policy details when making FAS requests, see [Forward access sessions](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_forward_access_sessions.html). 

## Service roles for Incident Manager
<a name="security_iam_service-with-iam-roles-service"></a>

**Supports service roles:** Yes

 A service role is an [IAM role](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles.html) that a service assumes to perform actions on your behalf. An IAM administrator can create, modify, and delete a service role from within IAM. For more information, see [Create a role to delegate permissions to an AWS service](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-service.html) in the *IAM User Guide*. 

**Warning**  
Changing the permissions for a service role might break Incident Manager functionality. Edit service roles only when Incident Manager provides guidance to do so.

### Choosing an IAM role in Incident Manager
<a name="security_iam_service-with-iam-roles-choose"></a>

When you create a response plan resource in Incident Manager, you must choose a role to allow Incident Manager to run a Systems Manager automation document on your behalf. If you have previously created a service role or service-linked role, then Incident Manager provides you with a list of roles to choose from. It's important to choose a role that allows access to run your automation document instances. For more information, see [Integrating Systems Manager Automation runbooks in Incident Manager for incident remediation](runbooks.md). When you create a Amazon Q Developer in chat applications chat channel to be used during an incident you can select a service role that allows you to use commands directly from chat. To learn more about creating chat channels for incident collaboration, see [Creating and integrating chat channels for responders in Incident Manager](chat.md). To learn more about IAM policies in Amazon Q Developer in chat applications, see [Managing permissions for running commands using Amazon Q Developer in chat applications](https://docs.aws.amazon.com/chatbot/latest/adminguide/chatbot-cli-commands.html#iam-policies-for-slack-channels-cli-support) in the *Amazon Q Developer in chat applications Administrator guide*.

## Service-linked roles for Incident Manager
<a name="security_iam_service-with-iam-roles-service-linked"></a>

**Supports service-linked roles:** Yes

 A service-linked role is a type of service role that is linked to an AWS service. The service can assume the role to perform an action on your behalf. Service-linked roles appear in your AWS account and are owned by the service. An IAM administrator can view, but not edit the permissions for service-linked roles. 

For information about creating or managing Incident Manager service-linked roles, see [Using service-linked roles for Incident Manager](using-service-linked-roles.md).
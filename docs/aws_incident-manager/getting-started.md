

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Getting started with Incident Manager
<a name="getting-started"></a>

This section walks through **Get prepared** in the Incident Manager console. You're required to complete **Get prepared** in the console before you can use it for incident management. The wizard walks you through setting up your replication set, at least one contact and one escalation plan, and your first response plan. The following guides will help you understand Incident Manager and the incident lifecycle:
+ [What Is AWS Systems Manager Incident Manager?](what-is-incident-manager.md)
+ [Incident lifecycle in Incident Manager](incident-lifecycle.md)

## Prerequisites
<a name="getting-started-prereq"></a>

If you're using Incident Manager for the first time, see the [Setting up AWS Systems Manager Incident Manager](setting-up.md). We recommend setting up Incident Manager in the account that you use to manage your operations.

We recommend that you complete the Systems Manager quick setup before beginning the Incident Manager **Get prepared** wizard. Use Systems Manager [Quick Setup](https://console.aws.amazon.com/systems-manager/quick-setup) to configure frequently used AWS services and features with recommended best practices. Incident Manager uses Systems Manager features to manage incidents associated with your AWS accounts and benefits from having Systems Manager configured first. 

## Get prepared wizard
<a name="getting-started-wizard"></a>

The first time you use Incident Manager, you can access the **Get prepared** wizard from the Incident Manager service homepage. To access the **Get prepared** wizard after you first complete setup, choose **Prepare** on the **Incidents** list page.

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home). 

1. On the Incident Manager service homepage, choose **Get prepared**. 

**General settings**

1. Under **General settings**, choose **Set up**.

1. Read the terms and conditions. If you agree to Incident Manager's terms and conditions, select **I have read and agree to the Incident Manager terms and conditions**, then choose **Next**.

1. In the **Regions** area, your current AWS Region appears as the first Region in your replication set. To add more Regions to your replication set, choose them from the list of Regions. 

   We recommend including at least two Regions. In case one Region is temporarily unavailable, incident-related activities can still be routed to the other Region.
**Note**  
Creating the replication set creates the `AWSServiceRoleforIncidentManager` service-linked role in your account. To learn more about this role, see [Using service-linked roles for Incident Manager](using-service-linked-roles.md).

1. To set up encryption for your replication set, do one of the following:
**Note**  
All Incident Manager resources are encrypted. To learn more about how your data is encrypted, see [Data protection in Incident Manager](data-protection.md). For more information about your Incident Manager replication set, see [Configuring the Incident Manager replication set](general-settings.md#replication).
   + To use an AWS owned key, choose **Use AWS owned key**.
   + To use your own AWS KMS key, choose **Choose an existing AWS KMS key**. For each Region you selected in step 3, choose an AWS KMS key, or enter an AWS KMS Amazon Resource Name (ARN). 
**Tip**  
If you don't have an available AWS KMS key, choose **Create an AWS KMS key**.

1. (Optional) In the **Tags** area, add one or more tags to the replication set. A tag includes a key and, optionally, a value.

   Tags are optional metadata that you assign to a resource. Tags allow you to categorize a resource in different ways, such as by purpose, owner, or environment. For more information, see [Tagging resources in Incident Manager](tagging.md).

1. (Optional) In the **Service access** area, to activate the Findings feature, choose the **Create service role for findings in this account** check box.

   A *finding* is information about a code deployment or infrastructure change that occurred around the same time that an incident was created. A finding can be examined as a potential cause of the incident. Information about these potential causes is added to the **Incident details** page for the incident. With information about these deployments and changes readily at hand, responders don't need to manually search for this information.
**Tip**  
To view information about the role to be created, choose **View permission details**.

1. Choose **Create**.

   To learn more about replication sets and resiliency, see [Resilience in AWS Systems Manager Incident Manager](disaster-recovery-resiliency.md).

**Contacts (Optional during **Get prepared**)**

Incident Manager engages contacts during an incident. For more information about contacts, see [Creating and configuring contacts in Incident Manager](contacts.md).

1. Choose **Create contact**. 

1. For **Name**, enter the contact's name.

1. For **Unique alias**, enter an alias to identify this contact.

1. In the **Contact channel** section., do the following to define how the contact is engaged during incidents:

   1. For **Type**, choose **Email**, **SMS**, or **Voice**.

   1. For **Channel name**, enter a unique name to help you identify the channel.

   1. For **Detail**, enter the email address or phone number for the contact.

      Phone numbers must have 9–15 characters and start with `+` followed by the country code and subscriber number.

   1. To create another contact channel, choose **Add contact channel**. We recommend defining at least two channels for each contact.

1. In the **Engagement plan** area, do the following to define which channels to notify the contact through, and how long to wait for an acknowledgement through each channel.
**Note**  
We recommend defining at least two channels in the engagement plan. 

   1. For **Contact channel name**, choose a channel you specified in the **Contact channel** area.

   1. For **Engagement time (min)**, enter the number of minutes to wait before engaging the contact channel. 

      We recommend that you select at least one device to engage at the beginning of an engagement, specifying **0** (zero) minutes waiting time.

   1. To add more contact channels to the engagement plan, choose **Add engagement**.

1. (Optional) In the **Tags** area, add one or more tags to the contact. A tag includes a key and, optionally, a value.

   Tags are optional metadata that you assign to a resource. Tags allow you to categorize a resource in different ways, such as by purpose, owner, or environment. For more information, see [Tagging resources in Incident Manager](tagging.md).

1. To create the contact record and send activation codes to the defined contact channels, choose **Create**.

1. (Optional) In the **Contact channel activation** page, enter the activation code sent to each channel.

   You can generate new activation codes later if you're not able to enter the codes now.

1. To add additional contacts, choose **Create contact** and repeat the preceding steps.

**(Optional during **Get prepared**) Escalation plans**

1. Choose **Create escalation plan**. 

   An escalation plan escalates through your contacts during an incident, ensuring that Incident Manager engages the correct responders during an incident. For more information about escalation plans, see [Creating an escalation plan for responder engagement in Incident Manager](escalation.md).

1. For **Name**, enter a unique name for the escalation plan.

1. For **Alias**, enter a unique alias to help you identify the escalation plan.

1. In the **Stage 1** area, do the following:

   1. For **Escalation channels**, choose contact channels to engage.

   1. If you want a contact to be able to halt the progression of escalation plan stages, select **Acknowledgment stops plan progression**.

   1. To add more channels to a stage, choose **Add escalation channel**.

1. To create a new stage in the escalation plan, choose **Add stage** and add its stage details.

1. (Optional) In the **Tags** area, add one or more tags to the escalation plan. A tag includes a key and, optionally, a value.

   Tags are optional metadata that you assign to a resource. Tags allow you to categorize a resource in different ways, such as by purpose, owner, or environment. For more information, see [Tagging resources in Incident Manager](tagging.md).

1. Choose **Create escalation plan**.

**Response plan**
**Note**  
You might need to return to the Incident Manager start page and choose **Prepare** to continue.

1. Choose **Create response plan**.

   Use the response plan to put together contacts and escalation plans you created.

   During this **Getting started** wizard, the following sections are optional, especially if this is your first time setting up a response plan:
   + **Chat channel**
   + **Runbooks** 
   + **Engagements**
   +  **Third-party integrations**

   For information about adding these elements to response plans later, see [Preparing for incidents in Incident Manager](incident-response.md).

1. For **Name**, enter a unique, identifiable name for the response plan. The name is used to create the response plan ARN or in response plans with no display name.

1. (Optional) For **Display name**, enter a name to help you identify this response plan when creating incidents.

1. For **Title**, enter a title to help identify the type of incident that relates to this response plan.

   The value you specify is included in each incident's title. The alarm or event that started the incident is also added to the title.

1. For **Impact**, select the impact level you expect for incidents related to this response plan, such as **Critical** or **Low**.

1. (Optional) For **Summary**, enter a brief description that is used to provide an overview of the incident. Incident Manager automatically populates relevant information into the summary during an incident.

1. (Optional) For **Dedupe string**, enter a dedupe string. Incident Manager uses this string to prevent the same root cause from creating multiple incidents in the same account.

   A deduplication string is a term or phrase the system uses to check for duplicate incidents. If you specify a deduplication string, Incident Manager searches for open incidents that contain the same string in the `dedupeString` field when it creates the incident. If a duplicate is detected, Incident Manager deduplicates the newer incident into the existing incident.
**Note**  
By default, Incident Manager automatically deduplicates multiple incidents created by the same Amazon CloudWatch alarm or Amazon EventBridge event. You don't have to enter your own deduplication string to prevent duplication for these resource types.

1. (Optional) In the **Incident Tags** area, add one or more tags to the response plan. A tag includes a key and, optionally, a value.

   Tags are optional metadata that you assign to a resource. Tags allow you to categorize a resource in different ways, such as by purpose, owner, or environment. For more information, see [Tagging resources in Incident Manager](tagging.md).

1. Select the contacts and escalation plans to apply to the incident from the **Engagements** dropdown.

1. Choose **Create response plan**. 

After you've created a response plan, you can associate Amazon CloudWatch alarms or Amazon EventBridge events with the response plan. This will automatically create an incident based on an alarm or event. For more information, see [Creating incidents automatically or manually in Incident Manager](incident-creation.md).
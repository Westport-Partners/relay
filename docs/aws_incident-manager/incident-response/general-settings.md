

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Configuring replication sets and Findings in Incident Manager
<a name="general-settings"></a>

After you have completed the Incident Manager Get prepared wizard, you can manage certain options on the **Settings** page. These options include your replication set, tags applied to the replication set, and the Findings feature.

**Topics**
+ [Configuring the Incident Manager replication set](#replication)
+ [Managing tags for a replication set](#general-tags)
+ [Managing the Findings feature](#settings-findings)

## Configuring the Incident Manager replication set
<a name="replication"></a>

The Incident Manager replication set replicates your data to many AWS Regions in order to do the following:
+ Increase cross-Region redundancy
+ Allow Incident Manager to access resources in different Regions and reduce latency for your users. 
+ Encrypt your data with either an AWS managed key or your own customer managed key. 

  All Incident Manager resources are encrypted by default. To learn more about how your resources are encrypted, see [Data protection in Incident Manager](data-protection.md). 

To get started with Incident Manager, first create your replication set using the **Get prepared** wizard. To learn more about getting prepared in Incident Manager, see the [Get prepared wizard](getting-started.md#getting-started-wizard).

### Editing your replication set
<a name="replication-edit"></a>

By using the Incident Manager **Settings** page, you can edit your replication set. You can add Regions, delete Regions, and enable or disable replication set deletion protection. You can't edit the key used to encrypt your data. To change the key, delete and recreate the replication set.

**Add a Region**

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home), and then choose **Settings** in the left navigation pane. 

1. Choose **Add Region**.

1. Select the **Region**. 

1. Choose **Add**.

**Delete a Region**

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home), and then choose **Settings** in the left navigation pane. 

1. Select the Region that you want to delete.

1. Choose **Delete**.

1. Enter **delete** into the text box, and choose **Delete**.

### Deleting your replication set
<a name="replication-delete"></a>

Deleting the last Region in your replication set deletes the entire replication set. Before you can delete the last Region, disable the deletion protection by turning off **Deletion protection** on the **Settings** page. After you delete your replication set, you can create a new replication set by using the **Get prepared** wizard. 

To delete a Region from your replication set, wait 24 hours after creating it. Attempting to delete a Region from your replication set sooner than 24 hours after creation causes the deletion to fail. 

Deleting your replication set deletes all Incident Manager data. 

**Delete the replication set**

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home), and then choose **Settings** in the left navigation pane. 

1. Select the last Region in your replication set.

1. Choose **Delete**.

1. Enter **delete** into the text box, and choose **Delete**.

## Managing tags for a replication set
<a name="general-tags"></a>

Tags are optional metadata that you assign to a resource. Use tags to categorize a resource in different ways, such as by purpose, owner, or environment.

**To manage tags for a replication set**

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home), and then choose **Settings** in the left navigation pane. 

1. In the **Tags** area, choose **Edit**.

1. To add a tag, do the following:

   1. Choose **Add new tag**.

   1. Enter a key and optional value for the tag.

   1. Choose **Save**.

1. To delete a tag, do the following:

   1. Under the tag you want to delete, choose **Remove**.

   1. Choose **Save**.

## Managing the Findings feature
<a name="settings-findings"></a>

The Findings feature helps responders in your organization identify potential root causes of incidents soon after the incidents begin. Currently, Incident Manager provides findings for AWS CodeDeploy deployments and AWS CloudFormation stack updates.

For cross-account support for findings, after you enable the feature, you must complete an additional setup step in each application account in the organization.

To use the feature, you let Incident Manager create a service role that includes the required permissions to access data on your behalf. 

**To enable the Findings feature**

1. Open the [Incident Manager console](https://console.aws.amazon.com/systems-manager/incidents/home), and then choose **Settings** in the left navigation pane. 

1. In the **Findings** area, choose **Create service role**.

1. 

   Review information about the service role to be created, and then choose **Create**.

**To disable the Findings feature**

To stop using the Findings feature, delete the `IncidentManagerIncidentAccessServiceRole` role from each account where it has been created.

1. Open the IAM console at [https://console.aws.amazon.com/iam/](https://console.aws.amazon.com/iam/).

1. In the left navigation pane, choose **Roles**.

1. In the search box, enter **IncidentManagerIncidentAccessServiceRole**.

1. Choose the name of the role, and then choose **Delete**.

1. Enter the role name in the dialog box to confirm that you want to delete the role, and then choose **Delete**.
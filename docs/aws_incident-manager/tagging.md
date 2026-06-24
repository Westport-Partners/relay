

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Tagging resources in Incident Manager
<a name="tagging"></a>

Tags are optional metadata that you can assign to your Incident Manager resources in the AWS Regions specified in your replication set. You can assign tags to response plans, incident records, and contacts. You can also add tags to on-call schedules and rotations.You can also add tags to the replication set itself. Tags enable you to categorize and control access to these resources in different ways. Each tag consists of a key and an optional value, both of which you define. We recommend that you devise a set of tag keys that meets your needs for each Incident Manager resource type. Using a consistent set of tag keys makes it easier for you to manage these resources and manage access to them. You can search and filter resources based on tags. For more information about controlling access to resources by using tags, see [Controlling access to AWS resources using tags](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_tags.html) in the *IAM User Guide*.

You can specify tags in the **Incident default** section when creating a response plan. These tags are applied to the incident record when an incident is created using the response plan.

**Note**  
Tags don't have any semantic meaning. They are interpreted strictly as a string of characters.

You can add or remove tags by using the Incident Manager console. The following screenshot displays the **Tags** area of a console page, with fields for adding tag keys and values, and buttons for adding and removing tags.

![A Tags section in Incident Manager console pages lets you add multiple tag key-value pairs to a resource.](http://docs.aws.amazon.com/incident-manager/latest/userguide/images/tags.png)


To work with tags programmatically, use the following API actions:
+ [TagResource](https://docs.aws.amazon.com/incident-manager/latest/APIReference/API_TagResource.html)
+ [UntagResource](https://docs.aws.amazon.com/incident-manager/latest/APIReference/API_UntagResource.html)
+ [ListTagsForResource](https://docs.aws.amazon.com/incident-manager/latest/APIReference/API_ListTagsForResource.html)

**Important**  
Tags applied to response plans, incident records, contacts, on-call schedules and rotations, and replication sets can be viewed and modified only from the resource owner account.
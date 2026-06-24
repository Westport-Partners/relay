

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Cleaning up Incident Manager Resources
<a name="migration-cleanup"></a>

If you are no longer using AWS Systems Manager Incident Manager, we recommend you clean up the remaining Incident Manager resources. This will fully offboard you from the service and prevent any ongoing charges. Please refer to the [AWS Systems Manager pricing page](https://aws.amazon.com/systems-manager/pricing/) for more details.

## Deleting the Replication Set
<a name="cleanup-replication-set"></a>

The Replication Set is a key component of Incident Manager that facilitates the replication of incident data across multiple AWS Regions. If you no longer require Incident Manager, you should delete the Replication Set.

To delete the Replication Set:

1. Open the AWS Systems Manager console

1. In the navigation pane, choose Incident Manager

1. Under "Replication Sets", locate the Replication Set you want to delete

1. Click on the Replication Set name to open the details page

1. On the Replication Set details page, click the "Delete" button

1. In the confirmation dialog, review the information and click "Delete Replication Set" to proceed with the deletion

**Note**  
Deleting the Replication Set will permanently remove all incident data stored in Incident Manager. Ensure that you no longer require access to any historical incident information before proceeding with the deletion.

## Deleting Incident Manager-related Resources
<a name="cleanup-resources"></a>

In addition to the Replication Set, you may have other Incident Manager-related resources, such as response plans, contacts, and runbooks. If you no longer require these resources, you can consider deleting them to fully offboard from Incident Manager.

To delete Incident Manager-related resources:

1. Open the AWS Systems Manager console

1. In the navigation pane, choose Incident Manager

1. Navigate to the appropriate section (e.g., "Response Plans", "Contacts", "Runbooks") and locate the resources you want to delete

1. Select the resources and click the "Delete" button to remove them
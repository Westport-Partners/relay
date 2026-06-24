

AWS Systems Manager Incident Manager is no longer open to new customers. Existing customers can continue to use the service as normal. For more information, see [AWS Systems Manager Incident Manager availability change](https://docs.aws.amazon.com/incident-manager/latest/userguide/incident-manager-availability-change.html). 

# Exporting Incident Manager data
<a name="export-data"></a>

This topic describes how to use a Python script to export incident records and post-incident analyses from AWS Systems Manager Incident Manager. The script exports data to structured JSON files for further analysis or archival purposes.

## What you can export
<a name="export-what"></a>

The script exports the following data:
+ Complete incident records, including:
  + Timeline events
  + Related items
  + Engagements
  + Automation executions
  + Security findings
  + Tags
+ Post-incident analysis documents from Systems Manager

## Prerequisites
<a name="export-prerequisites"></a>

Before you begin, make sure you have:
+ Python 3.7 or later installed
+ AWS CLI configured with appropriate credentials
+ The following Python packages installed:

  ```
  pip install boto3 python-dateutil
  ```

## Required IAM permissions
<a name="export-permissions"></a>

To use this script, make sure you have the following permissions:

Systems Manager Incidents permissions

```
{
    "Version": "2012-10-17",		 	 	 
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm-incidents:ListIncidentRecords",
                "ssm-incidents:GetIncidentRecord",
                "ssm-incidents:ListTimelineEvents",
                "ssm-incidents:GetTimelineEvent",
                "ssm-incidents:ListRelatedItems",
                "ssm-incidents:ListEngagements",
                "ssm-incidents:GetEngagement",
                "ssm-incidents:BatchGetIncidentFindings",
                "ssm-incidents:ListTagsForResource"
            ],
            "Resource": "*"
        }
    ]
}
```

Systems Manager permissions

```
{
    "Version": "2012-10-17",		 	 	 
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm:ListDocuments",
                "ssm:GetDocument",
                "ssm:GetAutomationExecution"
            ],
            "Resource": "*"
        }
    ]
}
```

## Export structure
<a name="export-structure"></a>

The script creates the following directory structure for exported data:

```
incident_manager_export_YYYYMMDD_HHMMSS/
├── incident_records/
│   ├── 20250309_102129_IAD_Service_A_Lambda_High_Latency.json
│   ├── 20250314_114820_SecurityFinding_SecurityHubFindings.json
│   └── ...
└── post_incident_analyses/
    ├── 20250310_143022_Root_Cause_Analysis_Service_A.json
    ├── 20250315_091545_Security_Incident_Review.json
    └── ...
```

## Running the export script
<a name="export-running"></a>

### Basic usage
<a name="export-basic"></a>

The Incident Manager data export script is provided `[here](samples/export-incident-manager-data.zip)`. Please download the script and use the following instructions to run the script.

To run the script with default settings:

```
python3 export-incident-manager-data.py
```

### Available options
<a name="export-options"></a>

You can customize the export using these command-line options:


| Option | Description | Default | 
| --- | --- | --- | 
| --region | AWS Region | us-east-1 | 
| --profile | AWS profile name | Default profile | 
| --verbose, -v | Enable detailed logging | FALSE | 
| --limit | Maximum number of incidents to export | No limit | 
| --timeline-events-limit | Maximum timeline events per incident | 100 | 
| --timeline-details-limit | Maximum timeline event details per incident | 100 | 
| --related-items-limit | Maximum related items per incident | 50 | 
| --engagements-limit | Maximum engagements per incident | 20 | 
| --analysis-docs-limit | Maximum analysis documents to export | 50 | 

### Examples
<a name="export-examples"></a>

Export from a specific Region using a custom profile:

```
python3 export-incident-manager-data.py --region us-east-1 --profile my-aws-profile
```

Export with verbose logging and limits for testing:

```
python3 export-incident-manager-data.py --verbose --limit 5 --timeline-events-limit 10
```

Export with conservative limits for large datasets:

```
python3 export-incident-manager-data.py --timeline-events-limit 50 --timeline-details-limit 25
```

## Output file structure
<a name="export-output"></a>

### Incident record JSON structure
<a name="export-incident-json"></a>

Each incident record file contains the following structure:

```
{
    "incident_record": {
        // Complete incident record from get-incident-record
    },
    "incident_summary": {
        // Incident summary from list-incident-records
    },
    "incident_source_details": {
        "from_incident_record": {},
        "from_incident_summary": {},
        "enhanced_details": {
            "created_by": "arn:aws:sts::...",
            "source": "aws.ssm-incidents.custom",
            "source_analysis": {
                "source_type": "manual",
                "creation_method": "human_via_console",
                "automation_involved": false,
                "human_created": true
            }
        }
    },
    "timeline_events": {
        "detailed_events": [
            {
                "summary": {}, // From list-timeline-events
                "details": {}  // From get-timeline-event
            }
        ],
        "summary_only_events": [],
        "metadata": {
            "total_events_found": 45,
            "events_with_details": 25,
            "limits_applied": {}
        }
    },
    "related_items": {
        "items": [],
        "metadata": {}
    },
    "engagements": {
        "engagements": [],
        "metadata": {}
    },
    "automation_executions": [],
    "findings": [],
    "tags": [],
    "post_incident_analysis": {
        "analysis_reference": {},
        "metadata": {}
    },
    "export_metadata": {
        "exported_at": "2025-09-18T...",
        "region": "us-east-*",
        "incident_arn": "arn:aws:ssm-incidents::..."
    }
}
```

### Post-incident analysis JSON structure
<a name="export-analysis-json"></a>

Each analysis document file contains:

```
{
    "document_metadata": {
        // Document metadata from list-documents
    },
    "document_details": {
        "Name": "037fc5dd-cd86-49bb-9c3d-15720e78798e",
        "Content": "...", // Full JSON content
        "DocumentType": "ProblemAnalysis",
        "CreatedDate": 1234567890,
        "ReviewStatus": "APPROVED",
        "AttachmentsContent": [],
        // ... other fields from get-document
    },
    "export_metadata": {
        "exported_at": "2025-09-18T...",
        "region": "us-east-*",
        "document_name": "..."
    }
}
```
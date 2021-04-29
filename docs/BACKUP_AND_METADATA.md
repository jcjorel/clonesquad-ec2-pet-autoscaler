# Backups and Metadata

CloneSquad comes with **configuration backup** and **metadata** export capabilities.

* The backup feature aims to avoid accidental loss of DynamoDB table contents (i.e. `CloneSquad-{GroupName}-Configuration` and `CloneSquad-{GroupName}-Scheduler` tables)
* The Metadata feature can be used to build a centralized *CMDB-like* to gain easy insights of numerous CloneSquad deployments among an AWS Organization (i.e. multi-account environment).

**Both features are enabled by the deployment parameter [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path).**

# Backups

When a backup is generated, the produced files are pushed into the `{MetadataAndBackupS3Path}/backups/accountid={AccountId}/region={Region}/groupname={GroupName}/` S3 folder:

* `latest-{AccountId}-{Region}-configuration-cs-{GroupName}.yaml`: YAML file export ready for import through [`configuration` API](INTERACTING.md#api-configuration)
* `latest-{AccountId}-{Region}-scheduler-cs-{GroupName}.yaml`: YAML file export ready for import through [`scheduler` API](INTERACTING.md#api-scheduler)

Backups are also archived with a time-based name at this location: `{MetadataAndBackupS3Path}/backups/accountid={AccountId}/region={Region}/groupname={GroupName}/archive/`.

> Important: It is recommended to set a [S3 lifecycle policy](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html) on this archive S3 path to avoid an infinite cumulative retention of backups.

As soon as the deployment parameter [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path) is defined, an hourly backup and metadata generation is automatically configured. This automation can be disabled by setting the key [`backup.cron`](CONFIGURATION_REFERENCE.md#backupcron) to `disabled`. This key can also be used to change the periodicity (See [scheduler documentation](SCHEDULER.md) for `cron()` and `localcron()` format).

A backup and metadata generation can also be triggered on-demand with the [`backup` API](INTERACTING.md#api-backup).


# Metadata (CMDB-like feature)

The metadata generation capability is an essential building block to ease governance of many CloneSquad deployment inside an AWS Organization. Properly configured to export their metadata in a shared centralized S3 bucket, the metadata produced by all CloneSquad deployments can be queried with [AWS Athena](https://aws.amazon.com/athena/) and provide a *single-pane of glass* solution to accomodate many management use-cases.

## Getting started with CloneSquad Metadata (CMDB)

1) Configure all your CloneSquad deployments with the same [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path) value pointing to a single shared S3 bucket (Bucket policy must allow CloneSquad deployments to push files in it). 
2) Got in the AWS Athena Console and execute each of the DDL scripts provided [in this directory](metadata/). **IMPORTANT: Take care to replace the DDL S3 Location template field with the value `{MetadataAndBackupS3Path}/metadata`** (ex: s3://cs-cmdb-bucket/somehierarchy/**metadata**)
3) **Do not forget** to call `MSCK REPAIR TABLE <table_name>;` each time a new CloneSquad deployment is performed (or configure an AWS Glue crawler to remediate this automatically).

## CMDB tables

The provided tables are:

* `clonesquad_discovery` table: Contains CloudFormation deployment parameters and a global context (like *Install time*, *AWS account name*, *AWS account email*...) of CloneSquad deployments. It also contains the JSON data provided by the user in [`UserSuppliedJSONMetadata`](DEPLOYMENT_REFERENCE.md#usersuppliedjsonmetadata).
* `clonesquad_configuration` table: Contains configurations of all CloneSquad deployements in a format that can be queried by AWS Athena,
* `clonesquad_scheduler` table: Contains scheduler definitions of all CloneSquad deployements in a format that can be queried by AWS Athena,
* `clonesquad_instances` table: Contains ec2.describle_instances() output as seen by all CloneSquad deployements. It is a data rich table about managed instances. As it is JSON based, user must deal with the nested nature of the records.
* `clonesquad_maintenace-windows` table: Contains SSM Maintenance Window objects applicable to all CloneSquad deployements.

Thanks to AWS Athena powerful query features, the user can build complex SQL statements joining and filtering one or all of these tables to get insights about effective usage of CloneSquad at AWS Organization scale.

Ex: Create the `displayallhostnames` view by joining the `clonesquad_discovery` and `clonesquad_instances` tables to display all managed instances by Display name, API GW URL controlling each them, CloneSquad version, etc...

```sql
CREATE OR REPLACE VIEW displayallhostnames AS
SELECT
  "_hostname" "Hostname"
, "interactapigwurl" as "InteractAPIGWUrl"
, "d"."CloneSquadVersion" as "CloneSquadVersion"
, "i"."accountid" as AccountId
, "d"."AccountName" as AccountName
, "d"."AccountEmail" as AccountEmail
, "i"."region" as Region
, "i"."groupname" as GroupName
, "i"."_subfleetname" as SubfleetName
FROM
  default.clonesquad_instances i
, default.clonesquad_discovery d
WHERE ((("i"."accountid" = "d"."accountid") AND ("i"."region" = "d"."region")) AND ("i"."groupname" = "d"."groupname"))
```

To show the view content, execute the following SQL statement in the AWS Athena console:

```sql
SELECT * FROM displayallhostnames;
```

Once your data are queriable by AWS Athena, user can benefit from all AWS Service integration (AWS Glue, AWS QuickSight...) and third-party tool (ex: PowerBI).




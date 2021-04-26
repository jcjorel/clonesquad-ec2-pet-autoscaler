# Backups and Metadata

CloneSquad comes with **configuration backup** and **metadata** export capabilities.

* The backup feature aims to avoid accidental loss of DynamoDB table contents (`CloneSquad-{GroupName}-Configuration` and `CloneSquad-{GroupName}-Scheduler` ones)
* The Metadata feature can be used to build a centralized *CMDB-like* to gain easy insights of numerous CloneSquad deployments among an AWS Organization (i.e. multi-account environment).

**Both features are enabled by the deployment parameter [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path).**

# Backups

When a backup is generated, the produced files are pushed into the `{MetadataAndBackupS3Path}/backups/accountid={AccountId}/region={Region}/groupname={GroupName}/` S3 folder:

* `latest-{AccountId}-{Region}-configuration-cs-{GroupName}.yaml`: YAML file export ready for import through [`configuration` API](INTERACTING.md#api-configuration)
* `latest-{AccountId}-{Region}-scheduler-cs-{GroupName}.yaml`: YAML file export ready for import through [`scheduler` API](INTERACTING.md#api-scheduler)

Backups are also archived with a time-based name at this location: `{MetadataAndBackupS3Path}/backups/accountid={AccountId}/region={Region}/groupname={GroupName}/archive/`.

> Important: It is recommended to set a [S3 lifecycle policy](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html) on this archive S3 path to avoid an infinite cumulative retention of backups.

As soon as the deployment parameter [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path) is defined, an hourly backup and metdata generation is automatically configured. This automation can be disabled by setting the key [`cron.backup`](CONFIGURATION_REFERENCE.md#cronbackup) to `disabled`. This key can also be used to change the periodicity (See [scheduler documentation](SCHEDULER.md) for `cron()` and `localcron()` format).

A backup and metadata generation can also be triggered on-demand with the [`backup` API](INTERACTING.md#api-backup).


# Metadata (CMDB-like feature)

The metadata generation capability is an essential building block to ease governance of many CloneSquad deployment inside an AWS Organization. Properly configured to export their metadata in a shared centralized S3 bucket, the metadata produced by all CloneSquad deployments can be queried with AWS Athena and provide a single-pane of glass solution to accomodate many management use-cases.

This [directory](docs/metadata/) contains AWS Athena DDL to configure basic Athena tables.

* `clonesquad_discovery` table: Contains CloudFormation deployment parameters and global context (Install time, AWS account name, AWS account email...) of CloneSquad deployment. It also contains the JSON data provided by the user in [`UserSuppliedJSONMetadata`](DEPLOYMENT_REFERENCE.md#usersuppliedjsonmetadata).
* 


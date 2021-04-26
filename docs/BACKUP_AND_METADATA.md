# Backup and Metadata

CloneSquad comes with **configuration backup** and **metadata** export capabilities.

* The backup feature aims to avoid accidental loss of DynamoDB table contents (`CloneSquad-{GroupName}-Configuration` and `CloneSquad-{GroupName}-Scheduler` ones)
* The Metadata feature can be used to build a centralized *CMDB-like* to gain easy insights of numerous CloneSquad deployments among an AWS Organization (i.e. multi-account environment).

**Both features are enabled by the deployment paramater [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path).**

# Configuration Backup

When a backup is generated, files are pushed in the *[`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path)/backups/accountid={AccountId}/region={Region}/groupname={GroupName}/* S3 folder:

* `latest-{AccountId}-{Region}-configuration-cs-test.yaml`: YAML file export ready for import through [`configuration` API](INTERACTING.md#api-configuration)
* `latest-{AccountId}-{Region}-scheduler-cs-test.yaml`: YAML file export ready for import through [`scheduler` API](INTERACTING.md#api-scheduler)

Backups are also archived with a time-based name at this location: *[`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path)/backups/accountid={AccountId}/region={Region}/groupname={GroupName}/archive/*

> Important: It is recommended to set a [S3 lifecycle policy](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html) on this archive S3 path to avoid an infinite cumulative retention of backups.

As soon as the deployment parameter [`MetadataAndBackupS3Path`](DEPLOYMENT_REFERENCE.md#metadataandbackups3path) is defined, an hourly backup and metdata generation is automatically configured. This automation can be disabled by setting the key [`cron.backup`](CONFIGURATION_MANAGEMENT.md#cronbackup) to `disabled`. This key can also be used to change the periodicity (See [scheduler documentation](SCHEDULER.md) for `cron()` and `localcron()` format).

A backup and metadata generaiton can also be triggered on-demand with the [`backup` API](INTERACTING.md#api-backup).


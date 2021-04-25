--WARNING: In the AWS Athena console, paste only one SQL statement at a time!
CREATE EXTERNAL TABLE clonesquad_discovery (
        `AlarmStateEC2Table` string,
        `ApiGWConfiguration` string,
        `ApiGwVpcEndpointDNSEntry` string,
        `CloneSquadVersion` string,
        `ConfigurationTable` string,
        `ConfigurationURLs` string,
        `CustomizationZipParameters` string,
        `DiscoveryLambdaIamRoleArn` string,
        `DynamoDBConfiguration` string,
        `EventTable` string,
        `InstallTime` timestamp,
        `InteractAPIGWUrl` string,
        `InteractApi` string,
        `InteractLambdaIamRoleArn` string,
        `InteractQueue` string,
        `InternalERRORInteractAlarmArn` string,
        `InternalERRORMainAlarmArn` string,
        `InternalWARNINGInteractAlarmArn` string,
        `InternalWARNINGMainAlarmArn` string,
        `LackOfCPUCreditAlarmArn` string,
        `LambdaMemorySize` string,
        `LogLevels` string,
        `LogRetentionDuration` string,
        `LoggingS3Path` string,
        `LongTermEventTable` string,
        `MainLambdaIamRoleArn` string,
        `MetadataAndBackupS3Path` string,
        `MetadataRecordLastUpdatedAt` timestamp,
        `PermissionsBoundary` string,
        `SchedulerTable` string,
        `StateTable` string,
        `Subfleets` array<string>,
        `TimeZone` string,
        `UserSuppliedJSONMetadata` string,
        `XRayDiagnosis` string
)
PARTITIONED BY (accountid string, region string, groupname string)
ROW FORMAT serde 'org.apache.hive.hcatalog.data.JsonSerDe'
LOCATION 's3://mybucket/mypath';
--After table creation, please run below SQL statement after each CloneSquad deployment.
MSCK REPAIR TABLE clonesquad_discovery;

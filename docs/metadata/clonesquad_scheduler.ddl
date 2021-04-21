--WARNING: In the AWS Athena console, paste only one SQL statement at a time!
CREATE EXTERNAL TABLE clonesquad_scheduler (
        `ExpirationTime` string,
        `Key` string,
        `MetadataRecordLastUpdatedAt` string,
        `Value` string
)
PARTITIONED BY (accountid string, region string, groupname string)
ROW FORMAT serde 'org.apache.hive.hcatalog.data.JsonSerDe'
LOCATION 's3://mybucket/mypath';
--After table creation, please run below SQL statement after each CloneSquad deployment.
MSCK REPAIR TABLE clonesquad_scheduler;

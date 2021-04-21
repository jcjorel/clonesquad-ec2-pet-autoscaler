CREATE EXTERNAL TABLE clonesquad_configuration (
        `Key` string,
        `MetadataRecordLastUpdatedAt` string,
        `Value` string
)
PARTITIONED BY (accountid string, region string, groupname string)
ROW FORMAT serde 'org.apache.hive.hcatalog.data.JsonSerDe'
LOCATION 's3://mybucket/mypath';
--MSCK REPAIR TABLE clonesquad_configuration;

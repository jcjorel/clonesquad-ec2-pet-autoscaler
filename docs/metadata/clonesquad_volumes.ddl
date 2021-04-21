CREATE EXTERNAL TABLE clonesquad_volumes (
        `Attachments` array<struct<
                `AttachTime`:timestamp,
                `DeleteOnTermination`:boolean,
                `Device`:string,
                `InstanceId`:string,
                `State`:string,
                `VolumeId`:string>>,
        `AvailabilityZone` string,
        `CreateTime` timestamp,
        `Encrypted` boolean,
        `Iops` bigint,
        `MultiAttachEnabled` boolean,
        `Size` bigint,
        `SnapshotId` string,
        `State` string,
        `Throughput` bigint,
        `VolumeId` string,
        `VolumeType` string
)
PARTITIONED BY (accountid string, region string, groupname string)
ROW FORMAT serde 'org.apache.hive.hcatalog.data.JsonSerDe'
LOCATION 's3://mybucket/mypath';
--MSCK REPAIR TABLE clonesquad_volumes;

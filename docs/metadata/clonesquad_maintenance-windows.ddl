CREATE EXTERNAL TABLE clonesquad_maintenance-windows (
        `Fleet` string,
        `IsMaintenanceTime` boolean,
        `MaintenanceWindows` array<struct<
                `Cutoff`:bigint,
                `Duration`:bigint,
                `Enabled`:boolean,
                `Name`:string,
                `NextExecutionTime`:timestamp,
                `Schedule`:string,
                `ScheduleTimezone`:string,
                `Tags`:array<struct<
                        `Key`:string,
                        `Value`:string>>,
                `WindowId`:string>>,
        `MetadataRecordLastUpdatedAt` timestamp,
        `NextMaintenanceWindowDetails` struct<
            `EndTime`:timestamp,
            `MatchingWindow`:struct<
                `Cutoff`:bigint,
                `Duration`:bigint,
                `Enabled`:boolean,
                `Name`:string,
                `NextExecutionTime`:timestamp,
                `Schedule`:string,
                `ScheduleTimezone`:string,
                `Tags`:array<struct<
                        `Key`:string,
                        `Value`:string>>,
                `WindowId`:string,
                `_FutureNextExecutionTime`:timestamp>,
            `MatchingWindowMessage`:string,
            `NextWindowMessage`:string,
            `StartTime`:timestamp>
)
PARTITIONED BY (accountid string, region string, groupname string)
ROW FORMAT serde 'org.apache.hive.hcatalog.data.JsonSerDe'
LOCATION 's3://mybucket/mypath';
--MSCK REPAIR TABLE clonesquad_maintenance-windows;

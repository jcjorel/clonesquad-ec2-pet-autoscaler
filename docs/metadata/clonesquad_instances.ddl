CREATE EXTERNAL TABLE clonesquad_instances (
        `AmiLaunchIndex` bigint,
        `Architecture` string,
        `BlockDeviceMappings` array<struct<
                `DeviceName`:string,
                `Ebs`:struct<
                    `AttachTime`:timestamp,
                    `DeleteOnTermination`:boolean,
                    `Status`:string,
                    `VolumeId`:string>>>,
        `CapacityReservationSpecification` struct<
            `CapacityReservationPreference`:string>,
        `ClientToken` string,
        `CpuOptions` struct<
            `CoreCount`:bigint,
            `ThreadsPerCore`:bigint>,
        `EbsOptimized` boolean,
        `EnaSupport` boolean,
        `EnclaveOptions` struct<
            `Enabled`:boolean>,
        `HibernationOptions` struct<
            `Configured`:boolean>,
        `Hypervisor` string,
        `IamInstanceProfile` struct<
            `Arn`:string,
            `Id`:string>,
        `ImageId` string,
        `InstanceId` string,
        `InstanceLifecycle` string,
        `InstanceType` string,
        `KeyName` string,
        `LaunchTime` timestamp,
        `MetadataOptions` struct<
            `HttpEndpoint`:string,
            `HttpPutResponseHopLimit`:bigint,
            `HttpTokens`:string,
            `State`:string>,
        `Monitoring` struct<
            `State`:string>,
        `NetworkInterfaces` array<struct<
                `Association`:struct<
                    `IpOwnerId`:string,
                    `PublicDnsName`:string,
                    `PublicIp`:string>,
                `Attachment`:struct<
                    `AttachTime`:timestamp,
                    `AttachmentId`:string,
                    `DeleteOnTermination`:boolean,
                    `DeviceIndex`:bigint,
                    `NetworkCardIndex`:bigint,
                    `Status`:string>,
                `Description`:string,
                `Groups`:array<struct<
                        `GroupId`:string,
                        `GroupName`:string>>,
                `InterfaceType`:string,
                `Ipv6Addresses`:array<string>,
                `MacAddress`:string,
                `NetworkInterfaceId`:string,
                `OwnerId`:string,
                `PrivateDnsName`:string,
                `PrivateIpAddress`:string,
                `PrivateIpAddresses`:array<struct<
                        `Association`:struct<
                            `IpOwnerId`:string,
                            `PublicDnsName`:string,
                            `PublicIp`:string>,
                        `Primary`:boolean,
                        `PrivateDnsName`:string,
                        `PrivateIpAddress`:string>>,
                `SourceDestCheck`:boolean,
                `Status`:string,
                `SubnetId`:string,
                `VpcId`:string>>,
        `Placement` struct<
            `AvailabilityZone`:string,
            `GroupName`:string,
            `Tenancy`:string>,
        `Platform` string,
        `PrivateDnsName` string,
        `PrivateIpAddress` string,
        `ProductCodes` array<string>,
        `PublicDnsName` string,
        `PublicIpAddress` string,
        `RootDeviceName` string,
        `RootDeviceType` string,
        `SecurityGroups` array<struct<
                `GroupId`:string,
                `GroupName`:string>>,
        `SourceDestCheck` boolean,
        `SpotInstanceRequestId` string,
        `State` struct<
            `Code`:bigint,
            `Name`:string>,
        `StateReason` struct<
            `Code`:string,
            `Message`:string>,
        `StateTransitionReason` string,
        `SubnetId` string,
        `Tags` array<struct<
                `Key`:string,
                `Value`:string>>,
        `VirtualizationType` string,
        `VpcId` string,
        `_Hostname` string,
        `_InstanceType` struct<
            `AutoRecoverySupported`:boolean,
            `BareMetal`:boolean,
            `BurstablePerformanceSupported`:boolean,
            `CurrentGeneration`:boolean,
            `DedicatedHostsSupported`:boolean,
            `EbsInfo`:struct<
                `EbsOptimizedInfo`:struct<
                    `BaselineBandwidthInMbps`:bigint,
                    `BaselineIops`:bigint,
                    `BaselineThroughputInMBps`:float,
                    `MaximumBandwidthInMbps`:bigint,
                    `MaximumIops`:bigint,
                    `MaximumThroughputInMBps`:float>,
                `EbsOptimizedSupport`:string,
                `EncryptionSupport`:string,
                `NvmeSupport`:string>,
            `FreeTierEligible`:boolean,
            `HibernationSupported`:boolean,
            `Hypervisor`:string,
            `InstanceStorageSupported`:boolean,
            `InstanceType`:string,
            `MemoryInfo`:struct<
                `SizeInMiB`:bigint>,
            `NetworkInfo`:struct<
                `DefaultNetworkCardIndex`:bigint,
                `EfaSupported`:boolean,
                `EnaSupport`:string,
                `Ipv4AddressesPerInterface`:bigint,
                `Ipv6AddressesPerInterface`:bigint,
                `Ipv6Supported`:boolean,
                `MaximumNetworkCards`:bigint,
                `MaximumNetworkInterfaces`:bigint,
                `NetworkCards`:array<struct<
                        `MaximumNetworkInterfaces`:bigint,
                        `NetworkCardIndex`:bigint,
                        `NetworkPerformance`:string>>,
                `NetworkPerformance`:string>,
            `PlacementGroupInfo`:struct<
                `SupportedStrategies`:array<string>>,
            `ProcessorInfo`:struct<
                `SupportedArchitectures`:array<string>,
                `SustainedClockSpeedInGhz`:float>,
            `SupportedBootModes`:array<string>,
            `SupportedRootDeviceTypes`:array<string>,
            `SupportedUsageClasses`:array<string>,
            `SupportedVirtualizationTypes`:array<string>,
            `VCpuInfo`:struct<
                `DefaultCores`:bigint,
                `DefaultThreadsPerCore`:bigint,
                `DefaultVCpus`:bigint,
                `ValidCores`:array<bigint>,
                `ValidThreadsPerCore`:array<bigint>>>,
        `_LastStartAttemptTime` timestamp,
        `_SubfleetName` string
)
PARTITIONED BY (accountid string, region string, groupname string)
ROW FORMAT serde 'org.apache.hive.hcatalog.data.JsonSerDe'
LOCATION 's3://mybucket/mypath';
--MSCK REPAIR TABLE clonesquad_instances;

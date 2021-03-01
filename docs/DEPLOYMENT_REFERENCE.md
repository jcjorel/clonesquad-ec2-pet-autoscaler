
# Deployment guidelines

This document describes configuration reference for CloudFormation [template.yaml](../template.yaml).

The template is designed to require a single mandatory parameter to deploy a working CloneSquad: `GroupName`.
All other parameters can be left with their respective defaults.

## `GroupName`

**Required: Yes**    
**Format: String**

The CloneSquad deployment is looking for resources with a `clonesquad:group-name` tag containing this value.
CloneSquad uses this tag/value to know which resources (EC2, RDS...) belongs to its duty.

## `LambdaMemorySize`

**Required: No**   
**Format: Integer**

Memory (MBytes) allocated to Main and Interact Lambda functions. 

* Minimum: 512 
* Maximum: 1792

As the Main Lambda function is mostly CPU bound, increasing memory to 1792 MBytes will ensure allocation of one full vCPU providing
the maximum possible compute power the Lambda functions. 

> **It is useless to give more memory than 1792 MBytes as the Main Lambda function is purely mono-threaded.**


## `ApiGWConfiguration`

**Required: No**   
**Format: MetaString**

	ApiGWConfiguration=[REGIONAL|PRIVATE],GWPolicy=<url_to_a_json_resource_file>,VpcEndpointDNS=vpce-0aaaaaaaaaaaaaaaa-bbbbbbbb.execute-api.eu-west-1.vpce.amazonaws.com

* [REGIONAL|PRIVATE]: (Optional) By default, the API Gateway is a REGIONAL one. This switch allows to define explictily if the API Gateway is private or public regional.
* GWPolicy: (Optional) Url to a customized API Gateway resource policy file. By default, the policy
[api-gw-default-policy.json](../src/resources/api-gw-default-policy.json) is automatically loaded. This default policy allows access only to `AWS_IAM`authenticated 
requests coming from the resource located in the AWS account where CloneSquad is deployed.
* VpcEndpointDNS: (Optional) When `ApiGWEndpointConfiguration`is not set, this value is used when a Api GW endpoint DNS name is required. **This parameter is especially useful when the endpoint is configured with `PrivateDnsEnabled` set to `False`.**

> Note: The Private API Gateway can't be accessed until some VPC Endpoints allows access to it.


## `ApiGWEndpointConfiguration`

**Required: No**   
**Format: MetaString**

	ApiGWEndpointConfiguration=VpcId=vpc-12345678,VpcEndpointPolicyURL=<url_to_policy_file>,SubnetIds=<subent_id_list>,TrustedClients=<list_of_rules>

This parameter controls the creation of VPC Endpoints to access the API Gateway from a specified VpcId.   

> By default, no VPC Endpoints are created: This is intended to allow CloneSquad deployment in an account
that uses VPC Sharing mechanism. When the API Gateway needs to be accessed from a VPC Shared, leave this field
empty and create manually the required VPC Endpoints from the AWS Account owning the shared VPC. 

* VpcId: (**Required**) VPC Id where to deploy VPC Endpoints to access the CloneSquad API Gateway.
* VpcEndpointPolicyURL: (Optional) Url to a VPC Endpoint policy file. By default, the policy 
[api-gw-default-endpoint-policy.json](../src/resources/api-gw-default-endpoint-policy.json). This policy allows `AWS_IAM` authenticated requests from specified
VpcId.
* SubnetIds: (Optional) Coma separated list of subnet Ids where to create a VPC Endpoint. By default, VPC Endpoints are deployed in all subnets of the specified VPcId. 
	- *Note: Comas MUST be backslashed!*
* PrivateDnsEnabled: (Optional) Default value is True.
* TrustedClients: (Optional) List of trusted sources for VPC Endpoint Security Group igress rules. Security groups, prefix lists and IP CIDR can be specified with a coma separated list. By default, 0.0.0.0/0 is defined as the igress rule.
	- *Note: Comas MUST be backslashed!*


## `LoggingS3Path`

**Required: No**   
**Format: String**

	LoggingS3Path=s3://<bucketname>/<objectpath>

Location where to send Debug reports.

When specified, on critical error (ex: Python exception), CloneSquad will generate a debug report as a Zip file that will be pushed in this S3 path.


## `UserNotificationArns`

**Required: No**   
**Format: StringList**

	UserNotificationArns=<Target notification ARN>,...

Coma separated list of notification targets. Can be Lambda, SQS or SNS ARNs.

## `ConfigurationURLs`

**Required: No**   
**Format: StringList**

	ConfigurationURLs=<Url_to_a_YAML_file>;...

Semicolon separated list of YAML files to load as configuration ones.

## `DynamoDBConfiguration`

**Required: No**   
**Format: MetaString**

By defaut, DynamoDB tables are configured to use On-Demand capacity provisionning. This parameter allows to switch to PROVISIONED capacity 
and so reduce costs. 

> Tip: Observe the tables metrics over a relevant period of time and determine the appropriate `ReadCapacityUnits` and 
`WriteCapacityUnits` for each tables. **WARNING: Do not make a table throttle by setting too low values as it will generate Python
exceptions preventing normal CloneSquad operations.**

Coma separated list of DynamoDB Table PROVISIONED throughput. Table name must be one of 
["ConfigTable", "AlarmStateEC2Table", "EventTable", "LongTermEventTable", "SchedulerTable", "StateTable"]

	ApiGWConfiguration=<TableName>=<ReadCapacityUnits>:<WriteCapacityUnits>,...
	Ex: ApiGWConfiguration=StateTable=3:3,EventTable=2:5

## `CustomizationZipParameters`

**Required: No**   
**Format: String**

	CustomizationZipParameters=<Zip customization file description>

Path to a ZIP file located in S3 expressed with the special format '<S3_bucket_name>:<S3_key_path>'.

## `TimeZone`

**Required: No**   
**Format: String**

	TimeZone=<TZ specification>

A time zone specification following the TZ format (ex: Europe/Paris, America/Los_Angeles...)

To list all valid timezones, use the following command:

	python3 -c "from dateutil.zoneinfo import get_zonefile_instance ; zonenames = print(get_zonefile_instance().zones.keys())"





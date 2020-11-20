
# Deployment guidelines

This document describes configuration reference for CloudFormation [template.yaml](../template.yaml).

The template is designed to require a single mandatory parameter to deploy a working CloneSquad: `GroupName`.
All other parameters can be left with their respective defaults.

## `GroupName`

**Required: Yes**    
**Format: String**

The CloneSquad deployment is looking for resources with a `clonesquad:group-name` tag containing this value.
CloneSquad uses this tag/value to know which resources (EC2, RDS...) belongs to its duty.

## `ApiGWConfiguration`

**Required: No**   
**Format: MetaString**

	ApiGWConfiguration=[REGIONAL|PRIVATE],GWPolicy=<url_to_a_json_resource_file>

* [REGIONAL|PRIVATE]: (Optional) By default, the API Gateway is a REGIONAL one. This switch allows to define explictily if the API Gateway is private or public regional.
* GWPolicy: (Optional) Url to a customized API Gateway resource policy file. By default, the policy
[api-gw-default-policy.json](../src/resources/api-gw-default-policy.json) is automatically loaded. This default policy allows access only `AWS_IAM`authenticated 
requests coming from the AWS account where CloneSquad is deployed.

> Note: The Private API Gateway can't be accessed until some VPC Endpoints allows access to it.


## `ApiGWEndpointConfiguration`

**Required: No**   
**Format: MetaString**

	ApiGWEndpointConfiguration=VpcId=vpc-12345678,

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

## `ConfigurationURL`

**Required: No**   
**Format: StringList**

	ConfigurationURL=<Url_to_a_YAML_file>,...

Coma separated list of YAML files to load as configuration files.

## `CustomizationZipParameters`

**Required: No**   
**Format: String**

	CustomizationZipParameters=<Zip customization file description>

Path to a ZIP file located in S3 expressed with the special format '<S3_bucket_name>:<S3_key_path>'.




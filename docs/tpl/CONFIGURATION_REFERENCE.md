

# CloneSquad Configuration reference

> This document is focused on runtime configuration of CloneSquad. See the specific documentation about the 
[CloneSquad deployment configuration](DEPLOYMENT_REFERENCE.md)

## Concepts

CloneSquad uses a multi-layered configuration system using the YAML semantic and format.

Each layer can override configuration defined in below layers.

Layer overrides by order of precedence (highest priority to lowest one):
1) DynamodDB configuration table [Parameter set](#parameter-sets),
2) DynamodDB configuration table,
3) CloudFormation template parameter '`ConfigurationURLs`' pointing to a list of URLs (see supported protocoles below),
4) YAML URLs listed in configuration key [`config.loaded_files`](#configloaded_files),
5) Built-in Defaults

URLs can use the following protocols: ["s3", "http", "https", "internal"]. 
* `internal:` references a file contained inside the Lambda filesystem,
* `s3://` references a file located into S3 with support for SigV4 authentication.

Note: If an URL resource fails to load, a warning is generated but it is safely ignored by the configuration subsystem to avoid
a service malfunction. Users needs to take into account this expected behavior.

### Customizing the Lambda package

The configuration subsystem allows reference to external URLs located in S3 or other Web servers. Some may have
concerns that it creates dependencies to resources that could be unreachable under a *Large Scale Event* condition 
(Ex: AZ unavailability).
In order to mitigate this concern, users can customize the Lambda package to embed their own configuration resources
and so, be able to access them with the reliable `internal:` protocol scheme.

To do so, create a ZIP file containing your YAML files and push it to an S3 Bucket accessible to CloudFormation. 

> Tip: In the ZIP file, create a file named 'custom.config.yaml' that will be read automatically at each scheduling Lambda function launch (every 20s by default).

The [Cloudformation template](../template.yaml) contains the parameter `CustomizationZipParameters` to inject this customization ZIP file at
deployment time.
* Format: <S3_bucket_name>:<S3_key_path_to_Zip_file>


### Parameter sets

The parameter set mechanism allows dynamic configuration override. It is mainly used to enable a whole
*named* bunch of configuration items with the single switch key [`config.active_parameter_set`](#configactive_parameter_set).

When set, the configuration subsystem is looking for a parameter set with the specified name.

A parameter set is a YAML dict (or using a special syntax in DynamoDB).

Ex:

	###
	# Define a parameter set key aside non-paramater set keys.
	ec2.schedule.min_instance_count: 2
	my-pset:
		ec2.schedule.min_instance_count: 3
		ec2.schedule.desired_instance_count: 50%
	# Activate the parameter set named 'my-pset' that will override matching non-parameter set keys.
	config.active_parameter_set: my-pset   

This example shows a dynamic override of the [`ec2.schedule.min_instance_count`](#ec2schedulemin_instance_count) and
[`ec2.schedule.desired_instance_count`](#ec2scheduledesired_instance_count) keys. The configuration subsystem will evaluate the 
key [`ec2.schedule.min_instance_count`](#ec2.schedule.min_instance_count) to a value of 3 (instead of 2) when [`config.active_parameter_set`](#configactive_parameter_set) is set to
'my-pset' value.

This mechanism is used in the demonstration [demo-scheduled-events](../examples/environments/demo-scheduled-events/). 
The CloneSquad scheduler is used to set the [`config.active_parameter_set`](#configactive_parameter_set) to temporarily activate a set of 
scaling parameters.

In the Configuration DynamoDB table, a specific syntax is used to describe key membership to a parameter set.

![Example of configuration DynamoDB table with parametersets](ConfigurationDynamoDBTable.png)

> Note: Every DynamoDB configuration keys starting by a character `#` are silently ignored (comment syntax).

## Configuration keys
{% for c in config %}

### {{ config[c].Key }}
Default Value: `{{ config[c].DefaultValue }}`   
Format       :  [{{ config[c].Format }}](#{{config[c].Format}})

{{ config[c].Description }}

{% endfor %}

## Configuration key formats

### Duration

A duration specification expressed in 2 possible ways:
* An [Integer](#Integer) representing a number of seconds,
* A [MetaString](#MetaString) following the meta keys as defined for the [timedelta object](https://docs.python.org/2/library/datetime.html#datetime.timedelta)

	Ex: `days=1,minutes=15,seconds=20` means 1 day + 15 minutes + 20 seconds

### MetaString

A way to represent a string and an associated dictionary of metadata in a one-liner format.

	Ex: <string_value>,<key1=val1>,<key2=val2>,<key3>

* Omitting the value part of a meta key imply the value 'True'
* If `string_value` has to contain a comma, it has to be escaped with a `\`

### MetaStringList

A list of [MetaString](#MetaString) separated by semi-colomn.

	Ex: <string_value1>,<key1_1=val1>,<key1_2=val2>,<key1_3>;<string_value2>,<key2_1=val2_1>

* Omitting the value part of a meta key imply the value 'True'
* If `string_valueX` has to contain a comma or a semi-column, it has to be escaped with a `\`

### StringList

A list of [String](#String) seperated by semi-column.

Currently, it is managed as a [MetaStringList](#MetaStringList) internally so StringList is an alias to
MetaStringList.

It may change in the future so do not specify meta data in keys flagged as [StringList](#StringList)

### IntegerOrPercentage

A [Integer](#Integer) value or a Percentage.

	Ex: `1`or `30%`

### String

A string...

### Integer

A number without fractional part (Ex: -1, 0, 1, 2... etc...)

### PositiveInteger

A positive [Integer](#Integer) including the `0`value.

### Bool

A boolean value represented with an [Integer](#Integer).

`0` means `False`. Other values mean `True`.



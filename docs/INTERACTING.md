
# Interacting with CloneSquad

CloneSquad comes with an Interaction API to perform both mutable and nonmutable actions.

There are 2 ways to interact with a CloneSquad deployment:
* An API Gateway (by default REGIONAL public; optionally PRIVATE - See [DEPLOYMENT_REFERENCE](DEPLOYMENT_REFERENCE.md#apigwconfiguration)),
* An SQS Queue.

Only the API Gateway is able to reply back an answer. The SQS queue can only be used to send command in an asynchronous manner
without reply channel.

These 2 resource URLs can identified from the CloudFormation outputs or dynamically with a dedicated Lambda discovery function.

Ex: 
```shell
# Discover the API Gateway URL
tmpfile=/tmp/cs-config.$$
aws lambda invoke --function-name CloneSquad-Discovery-${GroupName} --payload '' $tmpfile 1>/dev/stderr
APIGW_URL=$(jq -r '.["InteractAPIGWUrl"]' <$tmpfile)
rm -f $tmpfile
```

## SQS usage and message payload format

The SQS queue is protected by a security policy requiring that all allowed senders be listed in the `UserNotificationArns` Cloudformation template parameter.   
Note: This parameter can contain wildcards ("*" and "?")

	{
		"OpType": "<Interact_API_operation>",
		...
			<<Other operation specific parameters>>
		"Param1: "Value1",
		"Param2: "Value2",
		...
	}

## API Gateway usage

**Url format:**

	 https://<api_gateway_hostname>/v1/<Interact_API_operation>[?<Param1=Value1>&<Param2=Value2>]

If an operation takes parameters, they have to be passed as URL Query string.

The API gateway requires SiGV4 authentication (`AWS_IAM` authorizer) so you must present valid STS credentials to get access.
Using a tool like '[awscurl](https://github.com/okigan/awscurl)' (version 0.17 is known to work) or the Python `requests-iamauth` package can simplify 
this process or other AWS SDK managing as well with this kind of authentication.

> This API is designed with the assumption that it will be called from authenticated entities (ex: EC2 instances, Lambda function) 
that use service roles.

### Controlling acces to the API Gateway with IAM roles

As using `AWS_IAM` authenticated API calls, the API Gateway can control access to its resources while checking IAM roles used by the callers.

A [complete description of API gateway access crontrol possibilities](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-control-access-using-iam-policies-to-invoke-api.html) is available on the AWS site.

	{
	  "Version": "2012-10-17",
	  "Statement": [
	    {
	      "Effect": "Allow",
	      "Action": [
		"execute-api:Invoke"           
	      ],
	      "Resource": [
		"arn:aws:execute-api:region:<account-id>:pq264fab39/v1/GET/configuration/ec2.schedule.min_instance_count"
	      ]
	    }
	  ]
	}

Thanks to IAM policies, users can implement fined-grained access control to the CloneSquad API gateway resources (ex: Read-Only and Read-Write on a subset for instance).


# Interaction API operations

## API `metadata`

* Callable from : `API Gateway`

> Notice: **This API is only callable from a CloneSquad managed EC2 instance.**

By default, this API returns status related to the calling EC2 instance.

**Argument:**

* `instanceid`: (Optional) Do not guess the calling EC2 instance id and use the supplied one instead.

**Synopsis:**

	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/metadata
	{
		"AvailabilityZone": "eu-west-3a",
		"LocatedInAZWithIssues": false,
		"InstanceId": "i-0618fa840ca325b61",
		"State": "running",
		"Status": "ok",
		"SubfleetName": null,
		"SpotInstance": true,
		"SpotDetails": {
		    "InterruptedAt": null,
		    "RebalanceRecommendedAt": "2020-11-21 20:40:06.748674+00:00"
		}
		"Tags": [
		    {
			"Key": "Name",
			"Value": "MyInstanceName"
		    },
		    {
			"Key": "aws:ec2launchtemplate:version",
			"Value": "1"
		    },
		    {
			"Key": "aws:ec2launchtemplate:id",
			"Value": "lt-00000000000000000"
		    },
		    {
			"Key": "clonesquad:group-name",
			"Value": "test"
		    }
		]
	}

**Return value:**

* `AvailabilityZone`: Calling instance AvailabilityZone name 
* `LocatedInAZWithIssues`: Boolean indicating if this EC2 instance is located in an AZ signaled with issues (either manually or via the describe_availability_zones() EC2 API).
* `InstanceId`: Instance Id of the calling EC2 instance.
* `State`: Can be any of ["`pending`", "`running`", "`error`", "`bounced`", "`draining`"]
	* `pending`, `running` value comes from describe_instance EC2 API call and response field `["State"]["Name"]`
	* `error`is a CloneSquad specific value indicating that this instance failed to perform a critical operation requested by CloneSquad
(ex: a failed start_instance or other EC2 API call). This status indicates that this instance will be unmanaged during a period of time (5 minutes by default). For a running instance, this status doesn't prelude the fact that the instance is removed from any TargetGroup; it only means that CloneSquad won't attempt to start/stop it for a while
with the assumption the issue is transient.
	* `bounced` value means that this instance has been selected to be bounced as considered too aged by the bouncing algorithm. This instance is a synonym of `running`and is an advance advisory that the instance will be put in `draining` soon. The instance remains part of any participating TargetGroup so serving normally until formaly drained.
* `Status`: Can be any of ["`ok`", "`impaired`", "`insufficient-data`", "`not-applicable`", "`initializing`", "`unhealthy`", "`az_evicted`"]
	These field comes from describe_instance_status() EC2 API and returns the `["InstanceState"]["Name"]` response field for the instance.
	A special value `az_evicted` is added by CloneSquad to indicate that this instance is going to be evicted very soon as it is 
	running in an AZ with issues.
* `SubfleetName`: 'null' or name of the subfleet the instance belongs to.
* `Tags`: The describe_instance() EC2 API reponse field named `["Tags"]` for this instance.

## API `fleet/metadata`

* Callable from : `API Gateway`

This API returns a dict of the `metadata` structures for all managed EC2 instances.

**Synopsis:**

	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/fleet/metadata


## API `fleet/status`

* Callable from : `API Gateway`

This API dumps some synthetic status indicators. It contains indicators that can be used to follow the dynamic of status change 
in the CloneSquad fleets.

Example use-case: Track the fleet reaching 100% serving status.   
To perform an immutable update, user may set '`ec2.schedule.desired_instance_count`' to `100%` value to have all instances started.
This API can be polled to know when the whole fleet is started (`RunningFleetSize`) and ready (`ServingFleet_vs_MaximumFleetSizePourcentage` == `100`).

**API Gateway synopsis:**

	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/fleet/status
	{
		"EC2": {
		    "AutoscaledFleet": {
			"UnhealthyFleetSize": 0,
			"ManagedFleetSize": 20,
			"MaximumFleetSize": 20,
			"RunningFleetSize": 17,
			"ServingFleetSize": 17,
			"ServingFleet_vs_ManagedFleetSizePourcentage": 85,
			"ServingFleet_vs_MaximumFleetSizePourcentage": 85
		    },
		    "Subfleets": [
		        {
		    	"Name": "MySubfleetFleet",
			"RunningInstanceCount": 0,
			"RunningInstances": [],
			"StoppedInstanceCount": 2,
			"StoppedInstances": [
			    "i-0aaaaaaaaaaaaaaaa",
			    "i-0bbbbbbbbbbbbbbbb"
			],
			"SubfleetSize": 2
		    },
                    ...
                    ...
		    ]
		}
	}

**Return value:**

* **"AutoscaledFleet"**:
	* `UnhealtyFleetSize`: Number of instances that are reporting an unhealthy status. Note: Only instances in the autoscaled fleet are counted ; especially, subfleet instances are not part of this indicator.
	* `ManagedFleetSize`: Number of instances with the matching 'clonesquad:group-name' tag.
	* `MaximumFleetSize`: Maximum number of instances that can be running at this moment (This number excludes instances that CloneSquad
knows that it can't start now. Ex: Instance in `error`or `spot interrupted`).
	* `RunningInstances`: Number of instances with status 'pending' or 'running'.
	* `ServingFleetSize`: Number of instances that are running AND have passed all HealthChecks (either EC2 System or TargetGroup health check).
	* `ServingFleet_vs_ManagedFleetSizePourcentage`: int(100 * ServingFleetSize / MaximumFleetSize),
	* `ServingFleet_vs_MaximumFleetSizePourcentage`: int(100 * ServingFleetSize / ManagedFleetSize)
* **"Subfleets"**: List of subfleet structure
	* `Name`: Name of the subfleet,
	* `RunningInstances`: List of Instance Id member in 'pending' or 'running' state
	* `RunningInstanceCount`: length(`RunningInstances`)
	* `StoppedInstance`: Number of instances in 'stopped' state
	* `StoppedInstanceCount`: length(`StoppedInstances`)
	* `SubfleetSize`: Number of instances in the subfleet


## API `discovery`

* Callable from : `API Gateway`

**Synopsis:**

	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/discovery
	{
	    "discovery": {
		"AlarmStateEC2Table": "CloneSquad-test-AlarmState-EC2",
		"ConfigurationTable": "CloneSquad-test-Configuration",
		"ConfigurationURL": "",
		"EventTable": "CloneSquad-test-EventLog",
		"GroupName": "test",
		"InteractQueue": "https://sqs.eu-west-3.amazonaws.com/111111111111/CloneSquad-Interact-test",
		"LoggingS3Path": "s3://my-clonesquad-logging-bucketname/reports/",
		"LongTermEventTable": "CloneSquad-test-EventLog-LongTerm",
		"SchedulerTable": "CloneSquad-test-Scheduler",
		"StateTable": "CloneSquad-test-State"
	    },
	    "identity": {
		"accessKey": "AS------------------",
		"accountId": "111111111111",
		"caller": "AR-------------------:i-0ab1af6b52934bea1",
		"cognitoAuthenticationProvider": null,
		"cognitoAuthenticationType": null,
		"cognitoIdentityId": null,
		"cognitoIdentityPoolId": null,
		"principalOrgId": "o-oooooooooo",
		"sourceIp": "172.31.42.2",
		"user": "AR-------------------:i-0ab1af6b52934bea1",
		"userAgent": "python-requests/2.25.0",
		"userArn": "arn:aws:sts::111111111111:assumed-role/EC2AdminRole/i-0ab1af6b52934bea1",
		"vpcId": "vpc-11111111",
		"vpceId": "vpce-11111111111111111"
	    }
	}

**Return value:**
* `discovery`: A dict of Environment variables passed to the Interact Lambda function (see [template.yaml](../template.yaml)). This can used to locate various technical resources used by CloneSquad.
* `identity`: The `event["requestContext"]["identity"]` structure of the API Gateway Lambda context.

## API `notify/ackevent`

* Callable from : SQS Queue

This API is used to acknowledge a CloneSquad event and avoid their periodic repetition.

**SQS Payload synopsis:**

	{
		"OpType": "notify/ackevent",
		"EventData: ["<event['EventDate'] field taken form CloneSquad SQS event payload>"]
	}

A working example of use of this API is demonstrated [in this example](../examples/sam-sample-lambda/src/sample-clonesquad-notification/app.py#L36).

## API `configuration`

* Callable from : `API Gateway`

This API dumps (or upload) the whole CloneSquad configuration in JSON format by default (YAML format available on request).

**Argument:**

* `format`: (Optional) `json` or `yaml`.
* `raw`: (Optional) Dump the configuration in a format ready for subsequent import.
* `unstable`: (Optional) `true` or `false`. (Dump unstable configuration keys. **WARNING: Unstable configuration keys can be modified/suppressed between CloneSquad releases. Use them only for testing/debugging.**)

**API Gateway synopsis:**

	# Dump the current active configuration.
	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/configuration?format=json
	{
	    "app.disable": {
		"ConfigurationOrigin": "DynamoDB configuration table 'CloneSquad-test-Configuration'",
		"DefaultValue": 0,
		"Description": "Flag to disable Main Lambda function responsible to start/stop EC2 instances. \n\nIt disables completly CloneSquad. While disabled, the Lambda will continue to be started every minute to test\nif this flag changed its status and allow normal operation again.",
		"Format": "Bool",
		"Key": "app.disable",
		"Stable": true,
		"Status": "Key found in 'DynamoDB configuration table 'CloneSquad-test-Configuration''",
		"Value": "0"
	    },
	    ...
            ...
	}

	# Upload modifications to the active configuration (written in DynamoDB table).
	#    Note: The existing configuration is not replaced at once but merged
	# awscurl -X POST -d @configfile.yaml https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/configuration?format=yaml
	Ok (12 key(s) processed)

> Tip: **Setting a configuration key with an empty string value will delete the underlying DynamodDB table entry it it exists.**


## API `configuration/(.*)`

* Callable from : `API Gateway`

This API dumps and updates configuration on a per key basis.

**API Gateway synopsis:**

	# Dump the current value of configuration key 'ec2.schedule.min_instance_count'.
	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/configuration/ec2.schedule.min_instance_count
	2

	# Overwrite the configuration key 'ec2.schedule.min_instance_count' with value '3' (written in DynamoDB table).
	# awscurl -X POST -d 3 https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/configuration/ec2.schedule.min_instance_count
	3

> Tip: **Setting a configuration key with an empty string value will delete the underlying DynamodDB table entry it it exists.**


## API `scheduler`

* Callable from : `API Gateway`

This API acts on the DynamoDB Scheduler table and follows the same semantic than API `configuration` (see `configuration` documentation).

## API `scheduler/(.*)`

* Callable from : `API Gateway`

This API acts on the DynamoDB Scheduler table and follows the same semantic than API `configuration/(.*)` (see `configuration/(.*)` documentation).

## API `control/reschedulenow`

* Callable from : API Gateway and SQS Queue

This API triggers a manual resource scheduling in a specified time delay. It is especially useful when
`app.run_period` has a big value and user do not want to wait for the next planned rescheduling.

**Argument:**

* `delay`: (Optional) Number of seconds before rescheduling (must be 0 or positive). Default is `0`.

**API Gateway synopsis:**

	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/control/reschedulenow?delay=5
	On-demand rescheduling request acknowledged. Reschedule in 5 second(s)...

This call can be performed at any time (even if a scheduling is on-going). CloneSquad will also attempt to summarize
multiple requests sent through this API in a single rescheduling run if possible.

## API `cloudwatch/sentmetrics`

* Callable from : `API Gateway`

This API dumps the latest CloneSquad metrics sent to CloudWatch (These metrics are the one graphed by the CloneSquad supplied dashboard).

**API Gateway synopsis:**

	# Dump CloneSquad latest custom metrics.
	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/cloudwatch/sentmetrics
	[
	    {
		"Dimensions": [
		    {
			"Name": "GroupName",
			"Value": "test"
		    }
		],
		"MetricName": "RunningInstances",
		"StorageResolution": 60,
		"Timestamp": "2020-11-20 14:52:01.386924+00:00",
		"Unit": "Count",
		"Value": 17.0
	    },
	    {
		"Dimensions": [
		    {
			"Name": "GroupName",
			"Value": "test"
		    }
		],
		"MetricName": "PendingInstances",
		"StorageResolution": 60,
		"Timestamp": "2020-11-20 14:52:01.386924+00:00",
		"Unit": "Count",
		"Value": 0.0
	    },
	    ...
	    ...
	]


## API `cloudwatch/metriccache`

* Callable from : `API Gateway`

This API dumps the CloneSquad metric cache. This cache holds the metrics queried by CloneSquad mainly for autoscaling purpose but also
to monitor the 'CPU Credit' of managed burstable instances.

**API Gateway synopsis:**

	# Dump CloneSquad latest custom metrics.
	# awscurl https://pq264fab39.execute-api.eu-west-3.amazonaws.com/v1/cloudwatch/sentmetrics
	[
	    {
		"Id": "idxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
		"Label": "AWS/EC2 i-0aaaaaaaaaaaaaaaa CPUUtilization",
		"StatusCode": "Complete",
		"Timestamps": [
		    "2020-11-20 22:55:00+00:00",
		    "2020-11-20 22:54:00+00:00"
		],
		"Values": [
		    59.0,
		    57.0
		],
		"_MetricId": "CloneSquad-test-i-01111111111111111-00",
		"_SamplingTime": "2020-11-20 22:56:46.641023+00:00"
	    },
	    {
		"Id": "idyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
		"Label": "AWS/EC2 i-0bbbbbbbbbbbbbbbb CPUUtilization",
		"StatusCode": "Complete",
		"Timestamps": [
		    "2020-11-20 22:55:00+00:00",
		    "2020-11-20 22:54:00+00:00"
		],
		"Values": [
		    23.0,
		    23.0
		],
		"_MetricId": "CloneSquad-test-i-0bbbbbbbbbbbbbbbb-00",
		"_SamplingTime": "2020-11-20 22:56:46.641023+00:00"
	    },
	    ...
	    ...
	]



## API `debug/publishreportnow`

* Callable from : SQS Queue

This API triggers the generation of a Debug report to S3.

**SQS Payload synopsis:**

```json
        {
                "OpType": "Debug/PublishReportNow"
        }
```

An example of this API call can be seen in the command [cs-debug-report-dump](../tools/cs-debug-report-dump) that is used to manually
trigger the generation of a Debug report in S3.



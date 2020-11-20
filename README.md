
# CloneSquad, an AWS EC2 Pet Autoscaler
> An Autoscaler for mutable architectures on AWS [EC2](https://aws.amazon.com/what-is-cloud-computing/)

Because mutables architectures are still highly common and as they are encountered in most Cloud migrations, CloneSquad is a Serverless Autoscaler software with the main goal to get the most of the [Cloud benefits](https://aws.amazon.com/what-is-cloud-computing/) while taking the constraint
to never create or terminate [EC2](https://aws.amazon.com/ec2/) instances but only by doing start/stop of existing ones (aka **Pet** machines).

> CloneSquad is designed to be used when [AWS Auto Scaling](https://aws.amazon.com/autoscaling/) cannot be: It manages as well EC2 [ALB/NLBs](https://aws.amazon.com/elasticloadbalancing/), target groups and health checks mechanisms.


## Features and Benefits (Please also read the [FAQ](docs/FAQ.md))
* Scaling (see [Documentation details](docs/SCALING.md))
	- Automatic autoscaling based on [internal and/or user-defined alarms & metrics](docs/ALARMS_REFERENCE.md),
	- [Desired instance count](docs/CONFIGURATION_REFERENCE.md#ec2scheduledesired_instance_count) mode (ex: temporarily force 100% of instances to run and allow mutable update),
	- Always-on Availability Zone instance balancing algorithm,
	- Multi targetgroup support (associated to one or multiple ALB or NLB) at the same time (w/ smart instance draining before shutdown),
		* Note: CloneSquad can also work *without* any managed TargetGroup if not applicable to user use-case.
	- Automatic replacement of unhealthy/unavail/impaired instances,
	- (Optional) [Vertical scaling](docs/SCALING.md#vertical-scaling) (by leveraging instance type distribution in the fleet),
* Cost optimization
	- Support for 'persistent' [Spot instances](https://aws.amazon.com/ec2/spot/) aside of On-Demand ones in the same fleet with configurable priorities, Spot Rebalance recommendation and interruption handling,
	- Smart management of t[3|4].xxx burstable instances (aka '[CPU Crediting mode](docs/COST_OPTIMIZATION.md#clonesquad-cpu-crediting)' to avoid overcost linked to [unlimited bursting](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode.html)),
	- (Optional) ['LightHouse' mode](docs/SCALING.md#vertical-scaling) allowing to run automatically cheap instance types during low activity periods,
	- (Optional) Extra cost optimization option for non-autoscaled resources: [Static subfleet support](docs/SCALING.md#static-subfleet-support) both for EC2 Instances and RDS databases. Allows simple on/off use-cases (in combination with the scheduler. See [demonstration](examples/environments/demo-scheduled-events/)).
* Resilience
	- Manual or automatic Availability Zone eviction (automatic mode based on [*describe_availability_zones()* AWS standard API](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_availability_zones)),
	- (Optional) Instance bouncing: Frictionless fleet rebalancing and [AWS hypervisor maintenance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-instances-status-check_sched.html) by performing a permanent rolling state cycle (periodic start/stop of instances),
* Agility
	- Support for mixed instance type fleet,
	- Integrated [event scheduler](docs/SCHEDULER.md) ('cron' or 'rate' based) for complex scaling scenario,
	- Configuration hierarchy for complex dynamic settings,
	- [API Gateway](docs/INTERACTING.md#interacting-with-clonesquad) to monitor and make some basic operations.
* Observability
	- (Optional) CloudWatch dashboard (*Note: activated by default*),
	- [Events & Notifications](docs/EVENTS_AND_NOTIFICATIONS.md) (Lambda/SQS/SNS targets) framework to react to Squad events (ex: Register a just-started instance to an external monitoring solution and/or DNS),
	- [Extensive debuggability](docs/BUILD_RELEASE_DEBUG.md#debugging) with encountered scaling issues and exceptions exported to S3 (with contextual CloudWatch dashboard PNG snapshots).

## Installing / Getting started

Pre-requisites:
- An S3 bucket to upload the CloneSquad artifacts
- An EC2 instance with 'aws-cli', Docker installed and **an attached role allowing upload to the previously defined S3 bucket**

#### Step 1) Extract and Upload the latest CloneSquad CloudFormation template and associated artifacts

```shell
CLONESQUAD_VERSION=latest
CLONESQUAD_S3_BUCKETNAME="<your_S3_bucket_name_where_to_publish_clonesquad_artifacts>"
CLONESQUAD_S3_PREFIX="clonesquad-artifacts" # Note: Prefix MUST be non-empty
CLONESQUAD_GROUPNAME="test"
docker pull clonesquad/devkit:${CLONESQUAD_VERSION}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone | sed 's/\(.*\)[a-z]/\1/')}
# Upload template.yaml and CloneSquad Lambda functions to specified S3 bucket and prefix
docker run --rm clonesquad/devkit:${CLONESQUAD_VERSION} extract-version "${CLONESQUAD_S3_BUCKETNAME}" "${CLONESQUAD_S3_PREFIX}" latest s3
# Deploy a CloneSquad setup to manage GroupName=test (see Documentation for Group name concept)
aws cloudformation create-stack --template-url https://s3.amazonaws.com/${CLONESQUAD_S3_BUCKETNAME}/${CLONESQUAD_S3_PREFIX}/template.yaml \
    --stack-name MyFirstCloneSquad-${CLONESQUAD_GROUPNAME} \
    --capabilities '["CAPABILITY_NAMED_IAM","CAPABILITY_IAM","CAPABILITY_AUTO_EXPAND"]' \
    --parameter "[{\"ParameterKey\":\"GroupName\",\"ParameterValue\":\"${CLONESQUAD_GROUPNAME}\"}]"
aws cloudformation wait stack-create-complete --stack-name MyFirstCloneSquad-${CLONESQUAD_GROUPNAME}
```
> Note: If you get the error *'fatal error: Unable to locate credentials'*, you may have forgot to set a valid IAM role on the EC2 deployment instance.

This CloneSquad deployment is now ready to manage all EC2 instances and EC2 Targetgroups tagged with key *'clonesquad:group-name'* and value *'test'*.


You should see a `CloneSquad-test` dashboard in the CloudWatch console looking like this *(but blank, without any graphs)*:

![CloudWatch dashboard](examples/environments/demo-loadbalancers/scaling_demo_capture.png)

#### Step 2) Give to your CloneSquad deployment some EC2 instances and Targetgroups to manage

Next step is to create instances with this appropriate 'clonesquad:group-name' tag defined. For a quick demonstration using a fleet of 20 instances mixing Spot and 
On-Demand instances, go to [examples/environments/demo-instance-fleet](examples/environments/demo-instance-fleet). 
> In order to deploy this 
demonstration, you **MUST** configure the CloneSquad DevKit once and run the deploy script from within this container: See [instructions](docs/BUILD_RELEASE_DEBUG.md#configuring-the-devkit-to-start-demonstrations)!

Optional next step is to define also the tag *'clonesquad:group-name'* with value *'test'* on one or more EC2 targetgroups: CloneSquad will
automatically manage the membership of previousy created instances to these targetgroups. The [demo-loadbalancers](examples/environments/demo-loadbalancers/) demonstration is showing this.

## Initial Configuration

The default configuration has autoscaling in/out active and a directive defined to keep the serving fleet with, at least, 2 serving/healthy instances. Vertical scaling is disabled; 'LightHouse' mode as well. In this default configuration, CloneSquad does not make distinction between Spot and On-Demand instances managing them as an homogenous fleet.

Better benefits can be obtained by using [vertical scaling](docs/SCALING.md#vertical-scaling) and instance type priorities.

As general concept, the CloneSquad configuration can be done dynamically through a DynamoDB table or using a cascading set of YAML files located on a external Web servers, one or more S3 buckets requiring SigV4 authentication or finally directly integrated within the CloneSquad deployment for maximum resiliency toward external runtime dependencies. See [Configuration reference](docs/CONFIGURATION_REFERENCE.md) for more information.

## Costs

CloneSquad uses some AWS resources that will be billed at end of the month.

Below, some rough key figures to build an estimate:
* CloudWatch
	- Alarms: *((five_permanent_alarms) + (nb_of_serving_instances_at_a_given_time)) * 0.10$* per month
	- Dashboard: 1 x 3$ per month ([can be disabled](docs/CONFIGURATION_REFERENCE.md#cloudwatchdashboarduse_default))
	- Metrics: 
		* Up to 25 x CloudWatch metrics (0.10$ each) ~2.5$ per month (Metrics can be disabled individually with [`cloudwatch.metrics.excluded`](CONFIGURATION_REFERENCE.md#cloudwatchmetricsexcluded) to save costs. Metrics are also disabled if not applicable).
		* GetMetricData API call cost highly depends on number of running EC2 instances at a given time (as a rule of thumb, assume 1k requests per hour (=~7$ per month) when Squad is small/medium; assume more on large Squad and/or with intense and frequent scale out activities - CloneSquad increases dynamically its request rate toward CloudWatch under scale-out stress situation to increase its scaling decision accuracy).
* Lambda
	- The 'Main' Lambda function runs every 20 seconds by default for <4 seconds (~5$ per month)
* DynamoDB
	- Should be a few $ per month depending on scaling activities.
* X-Ray
	- Few cents per month (can be disabled)

**WARNING: The provided demonstrations deploy EC2 Instances with AWS Cloudwatch Log agents enabled that create tens of Custom metrics (RAM...).  These custom
metrics will generate a significant part of the demonstration bill and may not be considered as part of the CloneSquad cost.**

## Roadmap

* Improve documentation,
* Refine the IAM Role used by the CloneSquad Lambda that are far too wide,
* **Collect feedbacks from users about what they like/they do not like about CloneSquad.**,
	This early release is meant to understand and validate the original concept of CloneSquad (please send feedbacks to jeancharlesjorel@gmail.com)
* Think about an automatic testing capability (currently, tests are manuals),
* Implement a CI/CD pipeline for release (beyond the existing [release-everything script](scripts/release-everything)...),
* The API gateway responses are slow due to the way it is currently handled internally. Large ways of optimization are possible.
* There may be a cost benefit to move the 'Main' Lambda to an ECS Fargate Spot task. With limited effort, we could do this move while
keeping the 'Main' Lambda as automatic fallback in case of Spot interruption. To be investigated.


## Contributing

If you'd like to contribute, please fork the repository and use a feature
branch. Pull requests are warmly welcome.

## Licensing

The code in this project is licensed under [MIT license](LICENSE).


## Developping / Building / Releasing

See [dedicated documentation](docs/BUILD_RELEASE_DEBUG.md).




# CloneSquad, an AWS EC2 Pet Autoscaler

CloneSquad provides Elasticity to **Pet** EC2 instances: A single CloneSquad deployment can manage one **autoscaled** main fleet and any amount of *manually* scaled subfleets.

Per design, CloneSquad only starts and stops existing EC2 instances (i.e. it never creates or terminates instances): It uses a small set of tags to identify which EC2 instances are under its control.

> CloneSquad is designed to be used when [AWS Auto Scaling](https://aws.amazon.com/autoscaling/) cannot be: The EC2 instances in the **main autoscaled fleet** can leverage standard AWS [ALB/NLBs](https://aws.amazon.com/elasticloadbalancing/), target groups and health checks mechanisms.

CloneSquad is a ServerLess solution relying on CloudFormation, Lambda, SQS, SNS and DynamoDB AWS services: *It can be deployed as many times as needed **per AWS account** 
and **per region** depending on your autoscaling needs.*

## Features and Benefits (Please also read the [FAQ](docs/FAQ.md))

* Main fleet:
	- [Autoscaling](docs/SCALING.md) mode,
		* Automatically start/stop EC2 instances based on [CloudWatch alarms & metrics](docs/ALARMS_REFERENCE.md)
	- [Desired instance count](docs/CONFIGURATION_REFERENCE.md#ec2scheduledesired_instance_count) mode,
		* Define the precise amount of expected serving EC2 instances (specified in absolute or percentage)
	- Multi targetgroup support (associated to one or multiple ALB or NLB at the same time),
		* Note: CloneSquad can also work *without* any managed TargetGroup if not applicable to user use-case.
	- (Optional) ['LightHouse' mode](docs/SCALING.md#vertical-scaling) to run automatically cheap instance types during low activity periods,
	- (Optional) One dedicated CloudWatch dashboard.

* [Subfleet(s)](docs/SCALING.md#subfleet-support):
	- Manage groups of *EC2 Instances* **WITHOUT autoscaling need** (i.e. scaled manually based on time scheduling or other external scaling decision mechanisms and managed through the API Gateway),
		* [Desired instance count](docs/CONFIGURATION_REFERENCE.md#subfleetsubfleetnameec2scheduledesired_instance_count) is the only supported mode to control the amount of EC2 resources to start in each subfleet. 
	- Manage also *RDS databases* and *TransferFamily servers* (simple start/stop use-cases),
	- (Optional) One subfleet dedicated CloudWatch dashboard.

* Characteristics shared by all kinds of fleet:
	- (Optional) [Vertical scaling](docs/SCALING.md#vertical-scaling) (by leveraging *Spot'ness* and instance type distribution in the fleet),
	- **Always-on Availability Zone instance balancing algorithm**,
		* Ex: If multiple instances need to be running in a given fleet, CloneSquad will try to select instances evenly spread in different AZs.
	- Automatic replacement of unhealthy/unavail/impaired instances,
	- Support for [Maintenance Window and In-instance Event notifications with SSM (AWS System Manager)](docs/SSM.md)
	- Support for 'persistent' [Spot instances](https://aws.amazon.com/ec2/spot/) aside of On-Demand ones in the same fleet with **configurable priorities and Spot Rebalance recommendation/interruption handling**,
	- [Manual](docs/CONFIGURATION_REFERENCE.md#ec2azunavailable_list) or automatic instance eviction in case of an AWS Region outage affecting one or more AZs,
	- (Optional) **Instance bouncing**: Frictionless fleet rebalancing and [AWS hypervisor maintenance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-instances-status-check_sched.html) by performing a permanent rolling state cycle (periodic start/stop of instances),
	- (Optional) Smart management of *t[3|4].xxx* burstable instances (aka '[CPU Crediting mode](docs/COST_OPTIMIZATION.md#clonesquad-cpu-crediting)' to avoid overcost linked to [unlimited bursting](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode.html)),
	- Integrated [event scheduler](docs/SCHEDULER.md) ('cron' or 'rate' based) for complex scaling scenario,
	- [API Gateway](docs/INTERACTING.md#interacting-with-clonesquad) to monitor and manage a CloneSquad deployment,
	- [Events & Notifications](docs/EVENTS_AND_NOTIFICATIONS.md) (Lambda/SQS/SNS targets) framework enabling users to react to Squad events (ex: Register a just-started instance to an external monitoring solution and/or DNS),

> **IMPORTANT REMARK**: This solution aims to ease handling of **'Pet'** machines when you have to do so (very frequent after a pure *Lift&Shift*
migration into the Cloud). The author strongly advises to always consider managing resources as **'Cattle'** (with IaC, AutoScaling Groups, Stateless...).
**As a consequence, this solution should be only useful transient while transitionning to [Cloud Native best-practices](https://aws.amazon.com/getting-started/fundamentals-core-concepts/#Performance_Efficiency)**.

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
aws cloudformation create-stack --template-url https://s3.amazonaws.com/${CLONESQUAD_S3_BUCKETNAME}/${CLONESQUAD_S3_PREFIX}/template-${CLONESQUAD_VERSION}.yaml \
    --stack-name MyFirstCloneSquad-${CLONESQUAD_GROUPNAME} \
    --capabilities '["CAPABILITY_NAMED_IAM","CAPABILITY_IAM","CAPABILITY_AUTO_EXPAND"]' \
    --parameter "[{\"ParameterKey\":\"GroupName\",\"ParameterValue\":\"${CLONESQUAD_GROUPNAME}\"}]"
aws cloudformation wait stack-create-complete --stack-name MyFirstCloneSquad-${CLONESQUAD_GROUPNAME}
```
> Note: If you get the error *'fatal error: Unable to locate credentials'*, you may have forgot to set a valid IAM role on the EC2 deployment instance.

This CloneSquad deployment is now ready to manage all EC2 instances and EC2 Targetgroups tagged with key `clonesquad:group-name` and value `test`.


You should see a dashboard named `CS-test` in the CloudWatch console looking like this *(but blank, without any graphs)*:

![CloudWatch dashboard](examples/environments/demo-loadbalancers/scaling_demo_capture.png)

#### Step 2) Give to your CloneSquad deployment some EC2 instances and Targetgroups to manage

Next step is to create instances with this appropriate `clonesquad:group-name` tag defined. For a quick demonstration using a fleet of 20 instances mixing Spot and 
On-Demand instances, go to [examples/environments/demo-instance-fleet](examples/environments/demo-instance-fleet). 
> In order to deploy this 
demonstration, you **MUST** configure the CloneSquad DevKit once and run the deploy script from within this container: See [instructions](docs/BUILD_RELEASE_DEBUG.md#configuring-the-devkit-to-start-demonstrations)!

Optional next step is to define also the tag `clonesquad:group-name` with value `test` on one or more EC2 targetgroups: CloneSquad will
automatically manage the membership of previousy created instances to these targetgroups. The [demo-loadbalancers](examples/environments/demo-loadbalancers/) demonstration is showing this.

## Initial Configuration

The default configuration has all scaling activities disabled in all fleets. In the DynamoDB table `CloneSquad-test-Configuration`, to enable auto-scaling, create a record with these 2 items:
* `Key` with the string value `ec2.schedule.desired_instance_count` ,
* `Value` with the string value `-1`.

A pre-defined directive is already set to keep the autoscaled serving fleet with, at least, 2 serving/healthy instances. Vertical scaling is disabled; 'LightHouse' mode as well. In this default configuration, CloneSquad does not make distinction between Spot and On-Demand instances managing them as an homogenous fleet.

Better benefits can be obtained by using [vertical scaling](docs/SCALING.md#vertical-scaling) and instance type priorities.

As general concept, the CloneSquad configuration can be done dynamically through a DynamoDB table or using a cascading set of YAML files located on a external Web servers, S3 buckets requiring SigV4 authentication or finally directly embedded within the CloneSquad deployment for maximum resiliency toward external runtime dependencies. See [Configuration reference](docs/CONFIGURATION_REFERENCE.md) for more information.

## Costs

CloneSquad uses some AWS resources that will be billed at end of the month.

Below, some rough key figures to build an estimate:
* CloudWatch
	- Alarms: *((five_permanent_alarms) + (nb_of_serving_instances_at_a_given_time)) * 0.10$* per month
	- Dashboard: 2 x 3$ per month ([can be disabled](docs/CONFIGURATION_REFERENCE.md#cloudwatchdashboarduse_default))
	- Metrics: 
		* Up to 28 x CloudWatch metrics (0.10$ each) ~2.8$ per month (Metrics can be disabled individually with [`cloudwatch.metrics.excluded`](docs/CONFIGURATION_REFERENCE.md#cloudwatchmetricsexcluded) to save costs. Metrics are also disabled if not applicable). Additional metrics are created when using the subfleet feature.
		* GetMetricData API call cost highly depends on number of running EC2 instances at a given time (as a rule of thumb, assume 500 requests per hour (=~3$ per month) when Squad is small/medium; assume more on large Squad and/or with intense and frequent scale out activities.
* Lambda
	- The 'Main' Lambda function runs every 20 seconds by default for <4 seconds (~5$ per month)
* DynamoDB
	- Should be a few $ per month depending on scaling activities. See [`DynamodDBConfiguration`](docs/DEPLOYMENT_REFERENCE.md#dynamodbconfiguration)
parameter to configure DynamoDB tables in PROVISIONED capacity billing model instead of default PAY_PER_REQUEST to significantly reduce these costs if needed.
* X-Ray
	- Few cents per month (can be disabled)

**WARNING: The provided demonstrations deploy EC2 Instances with AWS Cloudwatch Log agents enabled that create tens of Custom metrics (RAM...).  These custom
metrics will generate a significant part of the demonstration CloudWatch bill and should not be considered as part of the CloneSquad cost.**

## Roadmap

* Improve documentation,
* Implement a CI/CD pipeline for release (beyond the existing [release-everything script](scripts/release-everything)...),


## Contributing

If you'd like to contribute, please fork the repository and use a feature
branch. Pull requests are warmly welcome.

## Licensing

The code in this project is licensed under [MIT license](LICENSE).


## Developping / Building / Releasing

See [dedicated documentation](docs/BUILD_RELEASE_DEBUG.md).



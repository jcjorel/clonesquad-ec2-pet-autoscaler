
Because mutables architectures are still highly common and as they are encountered in most Cloud migrations, CloneSquad is a Serverless Autoscaler software with the main goal to get the most of the [Cloud benefits](https://aws.amazon.com/what-is-cloud-computing/) while taking the constraint
to never create or terminate [EC2](https://aws.amazon.com/ec2/) instances but only by doing start/stop of existing ones (aka '**Pet**' machines).

> CloneSquad is designed to be used when [AWS Auto Scaling](https://aws.amazon.com/autoscaling/) cannot be: It manages as well EC2 [ALB/NLBs](https://aws.amazon.com/elasticloadbalancing/), target groups and health checks mechanisms. It contains various strategies to reduce cost of running EC2 Pet instance fleets.

Please visit the [GitHub repository](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler) for download and instruction

![CloudWatch dashboard](scaling_demo_capture.png)


# Features and Benefits (Please also read the [FAQ](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/FAQ.md))
* Scaling (see [Documentation details](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/SCALING.md))
	- Automatic autoscaling based on internal and/or user-defined alarms & metrics,
	- Desired instance count mode (ex: temporarily force 100% of instances to run and allow mutable update),
	- Always-on Availability Zone instance balancing algorithm,
	- Multi targetgroup support (associated to one or multiple ALB or NLB) at the same time w/ smart instance draining before shutdown),
	- Automatic replacement of unhealthy/unavail/impaired instances,
	- (Optional) [Vertical scaling](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/SCALING.md#vertical-scaling) (by leveraging instance type distribution in the fleet),
* Cost optimization
	- Support for 'persistent' [Spot instances](https://aws.amazon.com/ec2/spot/) aside of On-Demand ones in the same fleet with configurable priorities,
	- Spot interruption handling
	- Smart management of tX.xxx burstable instances (aka '[CloneSquad CPU Crediting](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/COST_OPTIMIZATION.md#clonesquad-cpu-crediting)' mode to avoid overcost linked to [unlimited bursting](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode.html)),
	- Dedicated Cloudwatch Alarm detecting burstable instances with CPU Credit exhausted,
	- Optional ['LightHouse' mode](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/SCALING.md#vertical-scaling) allowing to run automatically cheap instance types during low activity periods,
* Resilience
	- Manual or automatic Availability Zone eviction (automatic mode based on [*describe_availability_zones()* AWS standard API](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_availability_zones)),
	- (Optional) Instance bouncing: Frictionless [AWS hypervisor maintenance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-instances-status-check_sched.html) by performing a permanent rolling state cycle (periodic start/stop of instances),
	- (Optional) Static subfleet support both for EC2 Instances and RDS databases. Allows simple on/off use-cases (in combination with the scheduler for instance. See [demonstration](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/examples/environments/demo-scheduled-events/)).
* Agility
	- Support for mixed instance type fleet,
	- Integrated [event scheduler](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/SCHEDULER.md) ('cron' or 'rate' based) for complex scaling scenario,
	- Configuration hierarchy for complex dynamic settings,
	- API Gateway to monitor and make some basic operations.
* Observability
	- (Optional) CloudWatch dashboard (*Note: activated by default*),
	- [Events & Notifications](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/EVENTS_AND_NOTIFICATIONS.md) (Lambda/SQS/SNS targets) framework to react to Squad events (ex: Register a just-started instance to an external monitoring solution and/or DNS),
	- [Extensive debuggability](https://github.com/jcjorel/clonesquad-ec2-pet-autoscaler/blob/master/docs/BUILD_RELEASE_DEBUG.md#debugging) with encountered scaling issues and exceptions exported to S3 (with contextual CloudWatch dashboard PNG snapshots).


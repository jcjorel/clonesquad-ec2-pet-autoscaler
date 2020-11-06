

# Horizontal and Vertical scaling documentation


CloneSquad manages Horizontal and (optionally) Vertical scaling.

* The Horizontal scaling is the process of starting/stopping instances.
* The Vertical scaling is the process to leverage EC2 instance types for better scaling decisions.

**See the [demo-loadbalancers](../examples/environments/demo-loadbalancers) and [demo-scheduled-events](../examples/environments/demo-scheduled-events) to see Cloudwatch screenshots of scaling in action.**

## Horizontal scaling

The Horizontal scaling starts by tagging the target EC2 instance fleet with tag 'clonesquad:group-name' and value ${GroupName}.
The group name is the way CloneSquad knows which instances belongs to its management duties.

A special tag named 'clonesquad:excluded' when set to 'True', can be defined to temporarily exclude an instance from 
CloneSquad management.

Major configuration keys for horizontal scaling:
* [`ec2.schedule.min_instance_count`](CONFIGURATION_REFERENCE.md#ec2schedulemin_instance_count): The minimum number of instances to keep in serving *(!= running)* state in the fleet,
* [`ec2.schedule.desired_instance_count`](CONFIGURATION_REFERENCE.md#ec2scheduledesired_instance_count): The fixed number of serving instances at a given time (disable the autoscaler)

A serving instance has all its Health checks passed (if member of one or more target groups), is not in 'initializing' state and
has no system issues (i.e. impaired, unavailable etc...).

# Vertical scaling

The Vertical scaling is fully controlled by a single configuration key [`ec2.schedule.verticalscale.instance_type_distribution`](CONFIGURATION_REFERENCE.md#ec2scheduleverticalscaleinstance_type_distribution)
that defines a policy linked to fleet instance types, billing model and instance duties.

It can be used to favor Spot instances over On-Demand for instance or flag some instances as 'LightHouse' ones.

When activated, the LightHouse mode will inform the vertical scaler that some instances have a special duty: LightHouse instances
are designed to be small and so inexpensive. They are designed to 'Keep-the-light-on' when the Fleet has very limited activity. 
Highly assymetrical workloads during the day will benefit from 'LightHouse' instances. These instances will be automatically started
on ultra low activity and non LightHouse ones stopped providing improved cost optimization.

Ex: Sample vertical scaling policy

	t3.medium,count=3,lighthouse;c5.large,spot;c5.large;c5.xlarge

This is a [MetaStringList](CONFIGURATION_REFERENCE.md#MetaStringList) indicating to the vertical scaler algorithm
an order of scaleout preference based on instance types and billing models. The scalein algorithm uses the reverse order
of these preferences.

This example policy means:
* 3 x t3.medium instances are declared as LightHouse ones (Spot and On-Demand will match),
* First non LightHouse instances, all the c5.large **in Spot model** have the biggest priority,
* Then, all On-Demand c5.large are a priority level below,
* Finally, all c5.xlarge (Spot or On-Demand) have the lowest priority.

*Tip: It is recommend to define 3 LightHouse instances, one per AZ and [`ec2.schedule.min_instance_count`](CONFIGURATION_REFERENCE#ec2schedulemin_instance_count) set to the value 2 to optimize cost at most*

By default, CloneSquad will change the instance type of stopped instance to accomodate the policy (by calling the EC2.modify_instance_attribute() API).
In the above example, to determine which instance type have to change, the algorithm will first count fixed types:
* lighthouse instance have a 'count' keyword,
* c5.large,spot represents a fixed number as well as they are filtered as Spot and Spot can't have theit Instance Type changed.

Ex: The 20 instance fleet from the [demo-instance-fleet](../examples/environments/demo-instance-fleet/)

This fleet is defined like this:
* 3 x instances t3.medium in Spot model,
* 4 x instances c5.large in Spot model,
* All others are created as m5.large in the demonstration CloudFormation template.

Instance types can be changed only when an instance is stopped. So, over time, the scheduler will stop all these m5.large and 
you should see all them modified and spread evenly as c5.large and c5.xlarge to accomodate the policy.
All the Spot instances have their instance type left untouched as you can't change this attribute on Spot instances but all non-Spot instances
that do not match the policy will be modified dynamically as soon as they are stopped.

*Tip: Use vertical scaling with high instance type diversity only if your workload is able to leverage
a non-homogeneous fleet (ex: using [Least Outstanding Requests algorithm](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html#modify-routing-algorithm). If not, prefer this setup: one small/cheap instance type for LighHouse instances and a single instance 
type for non-LightHouse ones.*

# Static subfleet support

CloneSquad can manage static subfleets of EC2 and RDS instances. This support is added to complement the cost reduction benefits of the autoscaling feature.
The static subfleets are a set of resources using the special tag `clonesquad:static-subfleet-name` that will be managed differently than ones
not tagged this way.

This resources are managed in a On/Off manner through the dynamic configuration key named `staticfleet.{NameOfTheSubFleet}.state` which can take one
of these 3 values: [`stopped`, `undefined` or ``, `running`].

Ones can use the Scheduler to change this value to manage lifecycle of subfleets.

Ex:

	cron(0 7 * * ? *),staticfleet.mysubfleetname.state=running
	cron(0 19 * * ? *),staticfleet.mysubfleetname.state=stopped

Note: The static fleets resources have a dedicated widget in the CloneSquad dashboard. Notice that static resources are NOT part of graphed 
resources of others widgets. For instance, if a static fleet instance is entering 'CPU Crediting state', it won't appear in the 'TargetGroup and other statuses'
widget and you will only see 'draining' instances for a very long time in 'Static SubFleet statues' widget. For more details, look at logs!



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

# Subfleet support

CloneSquad can manage subfleets of EC2, RDS instances and TransferFamily servers. This support is added to complement the cost reduction benefits 
of the autoscaling feature provided by the main fleet.
The subfleets are sets of resources tagged with `clonesquad:group-name` but also with the special tag `clonesquad:subfleet-name`.

> When a resource is only tagged with `clonesquad:group-name`, it belongs to the Main fleet. When a resource is tagged **BOTH** with `clonesquad:group-name` **AND**
`clonesquad:subfleet-name`, it belongs to the specified subfleet.  

These subfleet resources are managed in a On/Off manner through the dynamic configuration key named `subfleet.<subfleetname>.state` which can take one
of these 3 values: [`stopped`, `undefined` (or ``), `running`].

Ones can use the CloneSquad Scheduler to change this value to manage lifecycle of subfleets.

Ex:
	# Start the subfleet named 'mysubfleetname' at 7AM UTC
	cron(0 7 * * ? *),subfleet.mysubfleetname.state=running
	# Stop the subfleet named 'mysubfleetname' at 7PM UTC
	cron(0 19 * * ? *),subfleet.mysubfleetname.state=stopped

EC2 resources are subject to an additional configuration key named [`subfleet.<subfleetname>.ec2.schedule.desired_instance_count`](CONFIGURATION_REFERENCE.md#subfleetsubfleetnameec2scheduledesired_instance_count) which have a 
similar semantic than in the Main fleet: It controls the number of running instances while [`subfleet.<subfleetname>.state`](CONFIGURATION_REFERENCE.md#subfleetsubfleetnamestate)
is set to `running`. Unlike with the main fleet parameter variant, the value `-1` is invalid (and so, does not mean autoscaling). When this parameter is set 
to a value different than `100%`, standard remediation mechanisms are activated (AZ instance balancing, faulty instance replacement, instance bouncing, 
instance eviction on faulty AZ, Spot interruption handling and replacement...)

Note: The subfleets resources have a dedicated widget in the CloneSquad dashboard. Notice that resources are NOT part of graphed 
resources of others widgets. For instance, if a subfleet instance is entering 'CPU Crediting state', it won't appear in the 'TargetGroup and other statuses'
widget and you will only see 'draining' instances for a very long time in 'SubFleet statues' widget. For more details, look at logs!

# About EC2 Spot instance support

CloneSquad monitors Spot instance interruption and rebalance recommandation EC2 events. 
* On 'rebalance recommendation', all Spot instances sharing the instance type and AZ signaled, are considered as unhealthy. If there are part of any TargetGroup
associated with, new instances will be launched to replace them. These new instances are selected by the autoscaler avoiding to start instances
with same characteritics (especially, it won't launch Spot instances sharing the signaled instance type and AZ.)
* On 'interruption', all signaled Spot instances are set to 'draining' state, removed from participating TargetGroups and replacement instances (not
sharing the same characterictics if possible) are started immediatly. 

CloneSquad can manage only one kind of Spot instances: 'Persistent' one with 'stop' behavior. Especially, Spot fleets ARE NOT supported and
will throw exceptions in the CloudWatch logs.

Below, the CloudFormation snippet to use and declare a CloneSquad compatible 'EC2 Launch Template'.

```yaml
    MyCloneSquadCompatibleSpotLaunchTemplate:
      Type: AWS::EC2::LaunchTemplate
      Properties:
        InstanceMarketOptions:
          MarketType: spot
          SpotOptions:
            InstanceInterruptionBehavior: stop
            SpotInstanceType: persistent
```



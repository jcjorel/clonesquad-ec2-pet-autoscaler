

# Horizontal and Vertical scaling documentation


CloneSquad manages Horizontal and (optionally) Vertical scaling.

* The Horizontal scaling is the process of starting/stopping instances.
* The Vertical scaling is the process to leverage EC2 instance types for better scaling decisions.

**See the [demo-loadbalancers](../examples/environments/demo-loadbalancers) and [demo-scheduled-events](../examples/environments/demo-scheduled-events) to see Cloudwatch screenshots of scaling in action.**

## Horizontal scaling

Using the Horizontal scaling starts by tagging the target EC2 instance fleet with tag `clonesquad:group-name` and value `${GroupName}` as specified in CloudFormation
template parameter.
The group name is the way for a CloneSquad deployement to know which instances belongs to its management duties.

A special tag named `clonesquad:excluded` when set to `True`, can be defined to temporarily exclude an instance from 
CloneSquad management.

Major configuration keys for horizontal scaling:
* [`ec2.schedule.min_instance_count`](CONFIGURATION_REFERENCE.md#ec2schedulemin_instance_count): The minimum number of instances to keep in serving *(!= running)* state in the fleet,
* [`ec2.schedule.desired_instance_count`](CONFIGURATION_REFERENCE.md#ec2scheduledesired_instance_count): The fixed number of serving instances at a given time (disable the autoscaler)

A serving instance has all its Health checks passed (if member of one or more target groups), is not in 'initializing' state and
has no system issues (i.e. impaired, unavailable etc...).

# Vertical scaling

The Vertical scaling, in the Main fleet, is fully controlled by a single configuration key [`ec2.schedule.verticalscale.instance_type_distribution`](CONFIGURATION_REFERENCE.md#ec2scheduleverticalscaleinstance_type_distribution) (and [`subfleet.<subfleetname>.ec2.schedule.verticalscale.instance_type_distribution`](CONFIGURATION_REFERENCE.md#subfleetsubfleetnameec2scheduleverticalscaleinstance_type_distribution) for subfleets)
that defines a policy linked to fleet instance types, billing model and instance duties.

It can be used to favor Spot instances over On-Demand as example or flag some instances as 'LightHouse' ones.

When activated, the LightHouse mode will inform the vertical scaler that some instances have a special duty: LightHouse instances
are designed to be small and so inexpensive. They are designed to '*Keep-the-light-on*' when the Fleet has very limited activity. 
Highly assymetrical workloads during the day will benefit from 'LightHouse' instances. These instances will be automatically started
on ultra low activity and non LightHouse ones stopped providing improved cost optimization.

Ex: Sample vertical scaling policy

	t3.medium,lighthouse;c5.large,spot;.*,spot;c5.large;c5.xlarge

This is a [MetaStringList](CONFIGURATION_REFERENCE.md#MetaStringList) indicating to the vertical scaler algorithm
an order of scaleout preference based on instance types and billing models. The scalein algorithm uses the reverse order
of these preferences.

This example policy means:
* t3.medium instances are declared as LightHouse ones (Spot and On-Demand will match),
* First non LightHouse instances, all the c5.large **in Spot model** have the biggest priority,
* Then, all remaining instances in **in Spot model** are scheduled next,
* Then, all On-Demand c5.large are a priority level below,
* Finally, all c5.xlarge (Spot or On-Demand) have the lowest priority.

Instance types can be expressed using a Regex format in the vertical scaling policy. 

A **typical and simple vertical policy to optimize costs at most** could look like this:

	(t2|t3|t4).*,lighthouse;.*,spot;.*

This policy means:
* All t2/t3/t4 instance types are LightHouse instances,
* All non-LightHouse instances **in Spot model** must be scheduled first,
* All other non-LightHouse instances are scheduled with the lowest priority.

Another **typical vertical policy to favor On-Demand over Spot instances**:

	.*,spot=False;.*

> **Remember that LightHouse instances are always optional!** You can omit to specify instance type for LightHouse if you do not want to use this feature!

### Tips

* It is recommended to define 3 LightHouse instances, one per AZ and [`ec2.schedule.min_instance_count`](CONFIGURATION_REFERENCE#ec2schedulemin_instance_count) set to the value 2 to optimize cost at most*

* Use vertical scaling with high instance type diversity only if your workload is able to leverage
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

	# SPECIAL SUBFLEET NAME `__all__` USAGE:
	# Force all defined subfleets to be in a running state at 3AM UTC with special subfleet name `__all__`
	cron(0 3 * * ? *),subfleet.__all__.state=running
	# Delete the key subfleet.__all__.state at 4AM UTC
	cron(0 4 * * ? *),subfleet.__all__.state=

EC2 resources are subject to an additional configuration key named [`subfleet.<subfleetname>.ec2.schedule.desired_instance_count`](CONFIGURATION_REFERENCE.md#subfleetsubfleetnameec2scheduledesired_instance_count) which have a 
similar semantic than in the Main fleet: It controls the number of running instances while [`subfleet.<subfleetname>.state`](CONFIGURATION_REFERENCE.md#subfleetsubfleetnamestate)
is set to `running`. When this parameter is set 
to a value different than `100%`, standard remediation mechanisms are activated (AZ instance balancing, faulty instance replacement, instance bouncing, 
instance eviction on faulty AZ, Spot interruption handling and replacement...)

Note: The subfleets resources have a dedicated widget in the CloneSquad dashboard. Notice that resources are NOT part of graphed 
resources of others widgets. For instance, if a subfleet instance is entering 'CPU Crediting state', it won't appear in the 'TargetGroup and other statuses'
widget and you will only see 'draining' instances for a very long time in 'SubFleet statues' widget. For more details, look at logs!

# About EC2 Spot instance support

CloneSquad monitors Spot instance interruption and rebalance recommandation EC2 events. 
* On 'rebalance recommendation' signal, signaled instances are considered as unhealthy and  
new instances are launched to replace them. Notice that even unhealthy, signaled Spot instances are NOT removed from any participating
TargetGroups. But, as any unhealthy instances, they will be drained and stopped after 15 minutes by default.
* On 'interruption' signal, all signaled Spot instances are set immediatly to 'draining' state, drained from participating TargetGroups and replacement instances 
are started immediatly. 

> **It is advised to use Spot instances spread among multiple AZs as it is more unlikely to have Spot starvation at the same time in all region AZs.**

CloneSquad can manage only one kind of Spot instances: '**Persistent**' one with 'stop' behavior. Especially, Spot 'fleet' instances ARE NOT supported and
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



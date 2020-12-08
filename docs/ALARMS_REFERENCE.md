
# CloneSquad Alarm Configuration Reference

## Concepts

CloneSquad uses Cloudwatch Alarms and associated underlying Cloudwatch metrics to take scaling decisions.

2 kinds of Cloudwatch Alarms can be specified:
1) CloneSquad managed alarms,
2) Externaly defined alarms tracked by CloneSquad.

For both kinds of Alarms, CloneSquad can react to and before an Alarm triggering following a point based system to 
value a scaling criteria.

The alarms are listed in the Configuration (See [Configuration concepts](CONFIGURATION_REFERENCE.md#concepts) to understand how to defined configuration) as a [MetaString](CONFIGURATION_REFERENCE.md#MetaString).

Example: 

	##
	# YAML file declaring an Alarm (load it with cs-kvtable tool or reference it as an ConfigurationURL)
	#
	cloudwatch.ec2.alarm00.configuration_url: internal:ec2.scaleup.alarm-cpu-gt-75pc.yaml,Points=1001,BaselineThreshold=0.0

This example declares the Alarm #00 using alarm specification described in internal file [ec2.scaleup.alarm-cpu-gt-75pc.yaml](../src/resources/ec2.scaleup.alarm-cpu-gt-75pc.yaml). Note: This file is part of the CloneSquad delivery inside the Main Lambda filesystem.   

The Alarm specification file uses a YAML format and will be passed directly to the [Cloudwatch.PutMetricAlarm API](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cloudwatch.html#CloudWatch.Client.put_metric_alarm). Any valid Cloudwatch alarm should be expressable in this YAML following the AWS PutMetricAlarm() documentation.

This example specifies also 2 meta information:
* **Points=1001**            : Override default points associated to an alarm (by default, 1000),
* **BaselineThreshold=0.0** : Specify a baseline threshold to allow CloneSquad works in analogous mode instead of relying only on Alarm triggering. In this example, it defines a baseline threshold of 0.0 (float). The alarm specification [ec2.scaleup.alarm-cpu-gt-75pc.yaml](../src/resources/ec2.scaleup.alarm-cpu-gt-75pc.yaml) defines a main Threshold of 75.0 (meaning 75 percent). CloneSquad will poll Cloudwatch underlying metric (i.e. CPU utilization) and generate points using the below formula:
> Tip: Even optional, it is strongly advised to always define a `BaselineThreshold` to avoid a jerky behavior of the autoscaler. 

Baselined point calculation:

	if AlarmTriggered:
	    GeneratedPoints = AlarmPoints
	elif BaselineThreshold defined and Cloudwatch.GetMetricData >= BaselineThreshold:
	    GeneratedPoints = AlarmPoints * (Cloudwatch.GetMetricData - BaselineThreshold) / (MainThreshold - BaselineThreshold) 
	else:
	    GeneratedPoints = 0.0

> Tip: The `BaselineThreshold` parameter should be set to a value that ensures a scaling criteria that is very close to zero (but ideally not zero) when the squad has no activity. It maximizes the autoscaling smoothness.


Note: Generated points can exceed AlarmPoints if the metric real data is above the main Threshold. CloneSquad algorithm will leverage this
calculation as a way to sense the urgency to scale out.



### Point based scaling criteria

Each Alarm is valued with 1000 points when it triggers. An Alarm that is not went off can also generate scaling points if a 
`BaselineThreshold` is specified (see previous paragraph).

The 'Point based scaling criteria` (aka PBSC Algorithm) is calculating a float scaling score using the following formula:

	Scaling criteria = [ Sum(NonTriggeredAlarmGeneratedPoints_withDividerMeta / AlarmSpecificDivider ) ] / 1000.0 + 
			[ Sum(NonTriggeredAlarmGeneratedPoints_withNoDividerMeta ) / NbOfServingInstances ] / 1000.0 + 
			[ Sum(TriggeredAlarmGeneratedPoints) ] / 1000.0

The autoscaler will scale out if the calculated *Scaling criteria* is above or equal to 1.0. It will scalein if
*Scaling criteria* is below 0.66. Between 0.66 and 1.0, the autoscaler will let the fleet as-is untouched.

The Cloudwatch Alarms are taken into account differently according their status:
* Alarms that are not in ALARM state (aka non-Triggered) are generating points **if a `BaselineThreshold`is defined**. If no
`BaselineThreshold` meta information is defined for a given alarm, it will generate points only when triggered.
* Alarms that are in ALARM state (aka Triggered) are generating their Base points (1000 points by default)

Each Alarm can have an optional `Divider` meta information used to control the influence of a specific alarm (more or less influence).   
Ex: 
Setting a `Divider` of 1 on a specific alarm will favor it in the overall calculation as others will be divided by the number of
serving instances (so less influent).

An example of use of this `Divider` meta is demonstrated in the [demo-loadbalancers](../examples/environments/demo-loadbalancers/configure-lb-responsetime-alarm.yaml) example directory. It defines 2 CloneSquad 
unmanaged alarms (tracking 2 LoadBalancer Response times). By setting to 1 the Divider, the Load balancer response times
will generate more points than alarms tracking the CPU Utilization defined on each instances by the directive `cloudwatch.ec2.alarm00.configuration_url` and, so, scaling decisions will be more influenced by the Load Balancer response
time that other alarm sources.

Note: Technically, in order to reduce Cloudwatch cost associated with GetMetricData API calls, the calculation *Sum(NonTriggeredAlarmGeneratedPoints_withNoDividerMeta ) / NbOfServingInstances* is working with cached data as much as possible synthetized by a weighted algorithm based on data age (recent data have more weight than older ones). This algorithm is
influenced by the [`cloudwatch.metrics.time_for_full_metric_refresh`](CONFIGURATION_REFERENCE.md#cloudwatchmetricstime_for_full_metric_refresh) parameter.

## Unmanaged alarms

CloneSquad can use Cloudwatch Alarms that are not created/terminated by itself to take scaling decisions. 
Typical use-case is to leverage *outside-of-the-fleet* metrics like response times of Load Balancers, length of SQS queues or 
Custom metrics specific to applications.

Ex:

	cloudwatch.ec2.alarmXX.configuration_url: alarmname:<Cloudwatch_alarm_name>[,BaselineThreshold=0.200[,Divider=1]]
	
The main difference with managed alarms is that there is no alarm specification YAML file; only a Cloudwatch Alarm name.

It is NOT an error to define a CloneSquad alarm pointing to a non-existing Cloudwatch alarm: It will be safely ignored.








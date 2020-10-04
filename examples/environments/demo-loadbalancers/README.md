
# Demo Load Balancers

This demo shows a set of LoadBalancers (2 ALBs and 1 NLB) and associated TargetGroups that are managed by
a CloneSquad deployement. 

Deploy the demonstration stack with this command:

	./deploy-test-loadbalancers.sh

The demonstration scenario is based on HTTP load generation on one of the Application Load Balancers and it tries to keep the
ResponseTime mean below 350ms. The managed instances must be from the [demo-instance-fleet](../demo-instance-fleet/)
**with vertical scaling enabled**.   

As it is a free autoscaling scenario (i.e `ec2.schedule.desired_instance_count` == -1), LightHouse instances (i.e
the 3 x t3.medium) will be part of scaling only at the beginning and will be quickly put out of service. Under scaleout
condition, it is expected that they won't start even if all non-LightHouse instances are started and overwhelmed by the load: 
LightHouse instances are usually too small to sustain a very high load and it is counter-productive to start them as last 
resort when the fleet is already saturated.

* If you just deployed the 'demo-instance-fleet' demonstration, the 3 x t3.medium instances will very quickly enter
the ['CPU Crediting'](../../../docs/COST_OPTIMIZATION.md#clonesquad-cpu-crediting) mode and it is expected. 
As a rule of thumb, just created burstable instances are expected to spent 8hrs in 'CPU Crediting' mode.
(See Cloudwatch dashboard and logs to see corresponding graphs and messages)

CloneSquad will be responsible for the registering/deregistering of instances.

CloneSquad detects dynamically which targetgroup(s) to manage by looking for a matching tag:
* Tag key: *"clonesquad:group-name"*
* Tag Value: *"${GroupName}"*

The demo template creates Cloudwatch alarms associated to `TargetResponseTime` LoadBalancer metrics. The Alarms are configured to trigger 
if response latency exceeds 350ms: The demo must show that latency is always kept below this value.

> Optionaly, it is **STRONGLY** advised to configure CloneSquad to track these LB metrics to take scaling decisions:
Users will be able to see visually the important benefits of using out-of-the-fleet metrics instead of relying only on 'CPU Utilization' instance metrics.
 
Configure LB tracking with this command:

```shell
${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${GroupName}-Configuration import <configure-lb-responsetime-alarm.yaml
```

The scripts [`perf.sh`](perf.sh) can be used to generate a sin wave load over one hour and half on one of the ALB. (Please contribute there a better load generator! Thanks!)

While running this demo **with Vertical Scaling enabled** *(from the 'demo-instance-fleet')*, you should see this kind of generated dashboard:

![demo scaling explained](scaling_demo_capture_explained.png)


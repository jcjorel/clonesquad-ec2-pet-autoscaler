
# FAQ

## Is CloneSquad a replacement of [AWS Auto Scaling](https://aws.amazon.com/autoscaling/)?

No. Functionally, CloneSquad relates to AWS Auto Scaling but it manages a corner-case that AWS Auto Scaling doesn't.
The Cloud best practice is to build and run immutable architectures so AWS Auto Scaling is definitively the good
choice in such modern environment.

CloneSquad features that look alike but are performed in a very different way than AWS Auto Scaling are:
* Instance fleet management,
* AWS EC2 Target groups management attached to ALB/NLB

## Can CloneSquad manage standard AWS ALB/NLB with associated Health Checks?

Indeed. CloneSquad main job is to register/deregister instance targets from any targetgroup with the
matching *'clonesquad:group-name'* tag.
This/These targetgroup(s) may or may not attached to ALB or NLB listeners doesn't matter. CloneSquad is
watching for targetgroup statuses to know when a target under its management is healthy or not.

> Tip: CloneSquad doesn't know anything about LoadBalancers: it only knows about targetgroups.

## CloneSquad is responsible for a critical characteristic of my system: 'Availability'. What if CloneSquad does not work as expected (bug, incorrect behavior...)?

CloneSquad never creates or terminates instances so the likely impact of a bug could be that too much or not enough instances are launched
or target groups are not properly managed.
We all know that under a stress situation, playbooks are essentials.

* If you think that's something is going bad with the autoscaler, disable it by setting a fixed instance count using [`ec2.schedule.desired_instance_count`](CONFIGURATION_REFERENCE.md#ec2scheduledesired_instance_count). **Setting the value to '100%' will launch all fleet instances and disable as well smart instance issue management ensuring full stability of the fleet at its maximum.**
* **If you suspect an important and critical issue with CloneSquad, immediate action would be to disable it with the configuration key [`app.disable`](CONFIGURATION_REFERENCE.md#appdisable) set to 1.**
	- This will disable all scheduling activities of CloneSquad and you will be able to manage again manually (through console or APIs) your 
instances and targetgroups.

If such event occurs, we would be glad to hear about it to improve CloneSquad. The software generates extensive debug reports
if the user configures the parameter `LoggingS3Path` in the [Cloudformation template](../template.yaml). It is recommended to
activate the debug reports by setting this parameter. 

See [Debugging CloneSquad](BUILD_RELEASE_DEBUG.md#debugging) for more information.

## I am a new CloneSquad user and I do not understand why burstable instances (t3/t4...) do not shutdown as expected. Is it normal?

Yes. The 'CPU Crediting mode' is activated by default. It means that CloneSquad will refuse to shutdown a t3/t4 instance that
do not gain at least 30% of daily accruable credits. It is one of the [cost optimization mechanisms](COST_OPTIMIZATION.md#clonesquad-cpu-crediting) 
available in CloneSquad. While in 'CPU Crediting mode', an instance does not participate to any TargetGroup to accrue credits in a low CPU condition.

If you want CloneSquad to disreguard the 'CPU Credit' status of burstabled instances, please set the configuration key
[`ec2.schedule.burstable_instance.max_cpu_crediting_instances`](CONFIGURATION_REFERENCE.md#ec2scheduleburstable_instancemax_cpu_crediting_instances) to `0%`. Once set, burstable instances
would be stopped immediatly as any other instance types.

## I'am NOT using AWS ALB/NLB/CLB but a Third-Party Load-Balancer to server requests toward my CloneSquad fleet. Are there any specific recommendations in this context?

Yes, there are. When CloneSquad does not manage TargetGroups (i.e. not serving instances with ALB/NLB/CLB), it does not have access to a builtin 'draining' mechanism. Said
differently, when CloneSquad is draining an EC2 instances and when used with TargetGroups, AWS mechanisms will take care to stop sending new connections to
drained instances and implement a smart and controlled grace period to finish serving the currently active ones.

When used with a Third-Party Load-Balancer, it is recommended to install the tool [`cs-instance-watcher`](TOOLS.md#cs-instance-watcher) on CloneSquad managed
EC2 instances. This tool can react to the instance going in 'draining' state by launching user-defined scripts and/or by forbidding new TCP connections
to a user defined list of ports. This latest option will help the Third-Party Load-Balancer detects that the 'draining' instances are unhealthy and should
be removed from the serving pool before the instances is effectively shutdown.


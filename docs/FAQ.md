
# FAQ

## Is CloneSquad a replacement of [AWS Auto-Scaling](https://aws.amazon.com/autoscaling/)?

No. Functionally, CloneSquad relates to AWS Auto Scaling but it manages a corner-case that AWS Auto Scaling doesn't.
The Cloud best practice is to build and run immutable architectures so AWS Auto Scaling is definitively the good
choice in such modern environment.

CloneSquad features that look alike but are performed in a very different way than AWS Auto-Scaling are:
* Instance fleet management,
* AWS EC2 Target groups management attached to ALB/NLB

## Can CloneSquad manage standard AWS ALB/NLB with associated Health Checks?

Indeed. CloneSquad main job is to register/deregister instance targets from any targetgroup with the
matching *'clonesquad:group-name'* tag.
This/These targetgroup(s) may or may not attached to ALB or NLB listeners doesn't matter. CloneSquad is
watching for targetgroup statuses to know when a target under its management is healthy or not.

> Tip: CloneSquad doesn't know anything about LoadBalancers: it only knows about targetgroups.


## Is Clonequad used in Production?

Not to our knowledge in this early release stage. We definitely want to hear CloneSquad users and understand what they are
doing with it.

## CloneSquad is responsible for a critical characteristic of my system: 'Availability'. What if CloneSquad does not work as expected (bug, incorrect behavior...)?

CloneSquad never creates or terminates instances so the likely impact of a bug could be that too much or not enough instances are launched
or target groups are not properly managed.
We all know that under a stress situation, run books are essentials.

* If you think that's something is going bad with the autoscaler, disable it by setting a fixed instance count using [`ec2.schedule.desired_instance_count`](CONFIGURATION_REFERENCE.md#ec2scheduledesired_instance_count). **Setting the value to '100%' will launch all fleet instances and disable as well smart instance issue management ensuring full stability of the fleet at its maximum.**
* **If you suspect an important and critical issue with CloneSquad, immediate action would be to disable it with the configuration key [`app.disable`](CONFIGURATION_REFERENCE.md#appdisable) set to 1.**
	- This will disable all scheduling activities of CloneSquad and you will be able to manage again manually (through console or APIs) your 
instances and targetgroups.

If such event occurs, we would be glad to hear about it to improve CloneSquad. The software generates extensive debug reports
if the user configures the parameter `LoggingS3Path` in the [Cloudformation template](../template.yaml). It is recommended to
activate the debug reports by setting this parameter. 

See [Debugging CloneSquad](BUILD_RELEASE_DEBUG.md#debugging) for more information.


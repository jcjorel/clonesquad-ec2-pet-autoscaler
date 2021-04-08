# SSM Support reference

[AWS System Manager](https://aws.amazon.com/systems-manager/) integration brings [Maintenance Window](#maintenance-window-support) support
and [In-instance Event notifications](#in-instance-event-notifications).

> **Important: By default, SSM support is disabled and must be explicitly enabled with [`ssm.enabled`](CONFIGURATION_REFERENCE.md#ssmenable).**

Even activated globally with [`ssm.enabled`](CONFIGURATION_REFERENCE.md#ssmenable), all features of the CloneSquad SSM module are disabled by default. One need to activate each feature explicitly with appropriate feature toggle.

## Maintenance window support

**Feature toggle:** [`ssm.feature.maintenance_window`](CONFIGURATION_REFERENCE.md#ssmfeaturemaintenance_window)

AWS SSM allows definition of up to 50 Maintenance Windows (MW) per account and region. Theses MWs are scheduled periods of time dedicated to perform maintenance actions (Patch management, Backup, etc...) on fleets of EC2 instances. 

CloneSquad extends native SSM Maintenance Window capabilities by looking at them as a source of scaling decisions and fleet behavior triggers. 

**During a Maintenance Window period**, the following statements are true:

* No instance can be put in `draining` state or even shutdown by CloneSquad,
	* As consequence, any instance started during the Maintenance Window period (manually through console or by the auto-scaler) remains up until end of the MW period (and Spot Interrupted instances are replaced as usual during a Maintenance Period).
* Managed EC2 instances MUST run a successfully registered SSM agent or will be considered as unhealthy otherwise (but won't be stopped until end of the Maintenance Window. cf. previous statement),
* By default, all managed instances (Main fleet instances -including LightHouse ones- or subfleet instances) are started. In this default temporary configuration (i.e. with `ec2.schedule.desired_instance_count` set to `100%`), no unhealthy instance replacement is performed leading to full fleet stability.

### Getting started with SSM Maintenance Window and CloneSquad

By default, CloneSquad expects to follow directions derived from SSM Maintenance Windows (MW) object named by convention.

Default SSM Maintenance Window naming convention:

* `CS-GlobalDefaultMaintenanceWindow`: The MW that will influence all CloneSquad deployments in an account/region,
	* `CS-{GroupName}`: A MW affecting all instances managed by the CS deployment with `clonesquad:group-name` == `{GroupName}`,
		* `CS-{GroupName}-Mainfleet`: A MW affecting only Main fleet instances,
	* `CS-{GroupName}-Subfleet.__all__`: A MW afffecting all subfleet instances,
		* `CS-{GroupName}-Subfleet.{SubfleetName}`: A MW affecting a specific instance fleet.

> **IMPORTANT: SSM Maintenance Window objects MUST be tagged with `clonesquad:group-name`: `{GroupName}` to be useable by CloneSquad.**

If multiple MW matches, they are cumulative (meaning effective maintenance window periods will be the union of all matching MWs).

By default, CloneSquad starts instances 15 minutes (see [`ssm.feature.maintenance_window.start_ahead`](CONFIGURATION_REFERENCE.md#ssmfeaturemaintenance_windowstart_ahead)) before the next MW period to ensure that instances are ready and stable when the SSM MW period effectively begins. The CloneSquad MW decisions are technically implemented by generating a temporary set of overriding settings (that can be seen by the user [through the API GW](INTERACTING.md#api-configuration)). At end of a MW period, these temporary scaling settings are removed and all user settings defined in CloneSquad configuration takes fully effect again. 

### Customizing behaviors during a Maintenance Window

One can change the default behaviors implied by a Maintenance Window period.

#### Tagging SSM Maintenance Objects to change default behaviors

Temporary MW settings can be modified through tags on the MW objects: All tags starting with the string `clonesquad:config:` will be considered as overriding directives.

By default, entering a MW period means that `ec2.schedule.min_instance_count` and `ec2.schedule.desired_instance_count` configuration settings are both temporary overriden with the string value `100%`: This makes all instances start (including LightHouse ones).

Tagging the MW object may be used to change these default settings (but also any possible settings as-well).

Example of tag names to set on a MW object:

	clonesquad:config:ec2.min_instance_count
	clonesquad:config:ec2.desired_instance_count
	clonesquad:config:subfleet.__all__.state

> **IMPORTANT: Due to tag value constraint, you can not use the `%` character to express a pourcentage. Please use the letter `p` as replacement** (Ex: `100p` means `100%`).


## In-instance Event notifications

CloneSquad is able to launch *Event scripts* hosted in managed instances running a SSM agent that sucessfully [registered to AWS SSM](https://docs.aws.amazon.com/systems-manager/latest/userguide/ssm-agent.html).

CloneSquad uses the [AWS SSM RunCommand](https://docs.aws.amazon.com/systems-manager/latest/userguide/execute-remote-commands.html) feature to upload in memory the [Linux helper script](../src/resources/cs-ssm-agent.sh) and launch scripts with expected names and location in the instance filesystem.

> Note: Sending events to Windows instances is currently not supported.

These event scripts allow user to react to some critical events to make operations smooth and reliable.
**These scripts are not meant to perform long running tasks** but to inform and probe about an event and associated return status if required. As a general rule of thumb, if a user script returns a zero-code, the event is assumed successfully taken into account by the instance. If the user scripts returns a non-zero code, the event will be repeated until event specific timeout or zero status code received.

> **IMPORTANT: All launched user scripts MUST execute in less than 30 seconds or will be forcibly terminated otherwise by the AWS SSM agent running in the EC2 instance.**  

### Notification of maintenance window period start/end

**Feature toggle:** [`ssm.feature.events.ec2.maintenance_window_period`](CONFIGURATION_REFERENCE.md#ssmfeatureeventsec2maintenance_window_period)

This event notifies an instance that it is entering/exiting a Maintenance Window period.

Scripts called depending on the event type:

* `/etc/cs-ssm/enter-maintenance-window-period`: Called when an instance enters a maintenance window period.
* `/etc/cs-ssm/exit-maintenance-window-period`: Called when an instance exits a maintenance window period.

> Note: A just started instance **always** receives ASAP this event to inform it what is the period type (i.e. this event is not only sent at the very moment of entering or exiting the maintenance window period).


### Probe of shutdown readyness

**Feature toggle:** [`ssm.feature.events.ec2.instance_ready_for_shutdown`](CONFIGURATION_REFERENCE.md#ssmfeatureeventsec2instance_ready_for_shutdown)

This event is sent as soon as an instance enter the 'draining' state. CloneSquad will wait for up to one hour (see [`ssm.feature.events.ec2.instance_ready_for_shutdown.max_shutdown_delay`](CONFIGURATION_REFERENCE.md#ssmfeatureeventsec2instance_ready_for_shutdownmax_shutdown_delay). A zero return code is expected from the user script `/etc/cs-ssm/instance-ready-for-shutdown` as prerequisite to shutdown the instance. After this delay, the instance is forcibly shutdowned.

A typical use-case for this event is to perform house keeping tasks and allow to shutdown instance gracefully. Examples of tasks can range from breaking the lifeline of loadbalancer healthchecks, wait for all active connections to terminate or backup the machine...

> Note: 15 mins before the end of the wait time (so, after 45 mins with default settings), the instance is placed in the 'unuseable' state. When an instance enters the 'unuseable' state, a User Notification of type `new_instances_marked_as_unuseable` is sent: User may log and react to this event to detect instances needing servicing. 


### Probe of operational readiness

**Feature toggle:** [`ssm.feature.events.ec2.instance_ready_for_operation`](CONFIGURATION_REFERENCE.md#ssmfeatureeventsec2instance_ready_for_operation)

This event is sent to probe if a just started instance is ready and can exit the 'initializing' state. If the user script `/etc/cs-ssm/instance-ready-for-operation` returns a zero code, CloneSquad assumes readiness and the instance is placed in 'running' state. 

When in 'initializing' state, an instance will never be stopped by CloneSquad. As a typical use-case, this event can by leveraged to ensure that an instance is assumed 'ready' only if it has completed its boot sequence. By using this event, you can avoid CloneSquad shutdowning down prematurely an instance with a very long boot time.

By default, CloneSquad waits up to one hour (see [`ssm.feature.events.ec2.instance_ready_for_operation.max_initializing_time`](CONFIGURATION_REFERENCE.md#ssmfeatureeventsec2instance_ready_for_operationmax_initializing_time)) to receive a zero return code. After this delay, the instance is set to 'unuseable' state and will be forcibly shutdown after 15 mins.


> Note: When an instance enters the 'unuseable' state, a User Notification of type `new_instances_marked_as_unuseable` is sent: User may log and react to this event to detect instances needing servicing. 




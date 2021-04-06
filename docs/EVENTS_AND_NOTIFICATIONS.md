
# Events and Notifications


Users can react to CloneSquad events:
* start_instances,
* stop_instances,
* register_targets,
* deregister_targets,
* instance_transitions,
* target_transitions,
* drain_instances,
* excluded_instance_transitions,
* spot_interruption_request,
* ssm_maintenance_window_event,
* start_db_cluster/stop_db_cluster,
* start_db_instance/stop_db_instance.

Each event has parameters allowing to retrieve the context of the event.

Each event receives at least the output of EC2.DescribeInstances() and EC2.DescribeTargetGroups() API call
as argument plus event specific information.

Lambda, SNS, SQS targets can be notified. Target ARNs must be comma separated in the
`UserNotificationArns` parameter of the [Cloudformation template](../template.yaml).

In order to provide the best reliability, CloneSquad send ongoing events periodically until they are acked or timed out.

An example of Lambda function [sample-lambda](../examples/sam-sample-lambda/) that receives
such notifications and ack each event received, is available.


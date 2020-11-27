
# Step #2: 'Production' Quick-Win 

## Additional context:

The Production is running at all time but the workload varies depending on the hour of the day and week day:
- Maximum load: 7AM-9AM and 1PM-3PM are peak loads all business days,
- Normal load: 6AM-9PM all business days,
- Low load: 9PM-6AM all business days,
- Ultra Low load: Tuesday and Sunday all day long.

From time-to-time, there are peak loads outside of the Maximum load periods.

> ACME would like a mean to fit resources to the observed periodic workloads but also an easy way to temporarily increase the resources on unexpected high load event.

## Proposed improvment using CloneSquad

For this use-case, the proposal is to use both scheduled and on-demand imperative scaling. 

* Find a naming convention for the production environment (ex: production)
* Deploy a CloneSquad CloudFormation template for production environment name. 
	- `GroupName` for the frontend: Value `<environment>`(ex: `production`)
	- See [README / Getting started](../README.md#installing--getting-started) for CloneSquad deployment instructions.
* Tag all the production resources (EC2 resources only) with their dedicated name (see previous bullet):
	- `clonesquad:group-name`: <GroupName> (ex: "prod1")
* Locate the two API Gateway URLs of Frontend and Backend CloneSquad deployemnt in their respective CloudFormation Outputs (parameter `InteractAPIWUrl`).
* Install [awscurl](https://github.com/okigan/awscurl) on the developper desktop (or best, manage the following steps through a CI tool like RunDeck, Jenkins...)
* Write a scaling shell scripts to be used by the orchestrator in the morning:

```shell
#!/bin/bash
SCALE_FOR_FRONTEND=$1  # Can be expressed as a number of instance or in percentage of available instances.
SCALE_FOR_BACKEND=$2
awscurl -X POST -d ${SCALE_FOR_FRONTEND} https://<frontend_api_gw_id>.execute-api.eu-west-1.amazonaws.com/v1/configuration/ec2.scheduler.desired_instance_count
awscurl -X POST -d ${SCALE_FOR_BACKEND} https://<backend_api_gw_id>.execute-api.eu-west-1.amazonaws.com/v1/configuration/ec2.scheduler.desired_instance_count
```

> In the orchestrator, before each test run, call the previous with expected size of frontend and backend instance fleet as first and second arguments.

* Look at the two 'CloneSquad-<GroupName>' CloudWatch dashboards to check that instances are starting/stopping as expected.






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
	- `GroupName` for the frontend: Value `<environment>-frontend`(ex: `production-frontend`)
	- `GroupName` for the frontend: Value `<environment>-backend`(ex: `production-backend`)
	- See [README / Getting started](../README.md#installing--getting-started) for CloneSquad deployment instructions.
* Tag all the production resources (EC2 resources only) with their dedicated name (see previous bullet):
	- `clonesquad:group-name`: <GroupName> (ex: "prod1-frontend" or "prod1-backend")
* Locate the two API Gateway URLs of Frontend and Backend CloneSquad deployemnt in their respective CloudFormation Outputs (parameter `InteractAPIWUrl`).
* Install [awscurl](https://github.com/okigan/awscurl) on a controlling host (or best, manage the following steps through a CI tool like RunDeck, Jenkins...)
* Write the configuration directive file `config.yaml`  for the predefined resource usage plan.
```yaml
pset-maximum-load:
  # all instances serving 
  ec2.schedule.desired_instance_count: 100%
pset-normal-load:
  # 60% of instances serving 
  ec2.schedule.desired_instance_count: 60%
pset-low-load:
  # 3 instances serving spread over 3 different AZs (CloneSquad always ensures AZ balancing automatically)
  ec2.schedule.desired_instance_count: 3
pset-ultra-low-load:
  # 2 instances serving over 2 different AZs (CloneSquad always ensures AZ balancing automatically)
  ec2.schedule.desired_instance_count: 2
ec2.schedule.min_instance_count: 2
```
* Load this configuration through the 2 APIs (Note: different configurations could be uploaded for 'frontend' and 'backend' if needed)
```shell 
 awscurl -X POST -d @config.yaml https://<frontend_api_gw_id>.execute-api.eu-west-3.amazonaws.com/v1/configuration?format=yaml
Ok (5 key(s) processed)
 awscurl -X POST -d @config.yaml https://<backend_api_gw_id>.execute-api.eu-west-3.amazonaws.com/v1/configuration?format=yaml
Ok (5 key(s) processed)
```
* Write the scheduler directive file `cron.yaml` for the resource scaling plan.
```yaml
# At 7AM, we switch in maximum load configuration
business-day-maximum-load-start1: "cron(0 7 ? * MON-FRI *),config.active_parameter_set=pset-maximum-load" 
# At 9AM, we switch back in normal load configuration
business-day-maximum-load-stop1: "cron(0 9 ? * MON-FRI *),config.active_parameter_set=pset-normal-load" 
# At 1PM, we switch again in maximum load configuration
business-day-maximum-load-start2: "cron(0 13 ? * MON-FRI *),config.active_parameter_set=pset-maximum-load" 
# At 3PM, we switch back agin in normal load configuration
business-day-maximum-load-stop2: "cron(0 15 ? * MON-FRI *),config.active_parameter_set=pset-normal-load" 
# At 9PM, we switch in low load configuration
business-day-low-load-start: "cron(0 21 ? * MON-FRI *),config.active_parameter_set=pset-low-load"
# At 6AM, we switch in normal load configuration for a new business day cycle.
business-day-low-load-end: "cron(0 6 ? * MON-FRI *),config.active_parameter_set=pset-normal-load" 

# On Week-end we switch in ultra low activity configuration.
weekend: "cron(1 0 ? * SAT *),config.active_parameter_set=pset-ultra-low-load" 
```
* Load this configuration through the 2 APIs (Note: different configurations could be uploaded for 'frontend' and 'backend' if needed)
```shell 
 awscurl -X POST -d @cron.yaml https://<frontend_api_gw_id>.execute-api.eu-west-3.amazonaws.com/v1/scheduler?format=yaml
Ok (6 key(s) processed)
 awscurl -X POST -d @cron.yaml https://<backend_api_gw_id>.execute-api.eu-west-3.amazonaws.com/v1/scheduler?format=yaml
Ok (6 key(s) processed)
```

## On-Demand scaling

* On unplanned peak load, force the fleet with the desired configuration.

```shell
#!/bin/bash
EXPECTED_CONFIGURATION=pset-maximum-load
awscurl -X POST -d ${EXPECTED_CONFIGURATION} https://<frontend_api_gw_id>.execute-api.eu-west-1.amazonaws.com/v1/configuration/config.active_parameter_set
awscurl -X POST -d ${EXPECTED_CONFIGURATION} https://<backend_api_gw_id>.execute-api.eu-west-1.amazonaws.com/v1/configuration/config.active_parameter_set
```

* Always look at the two 'CloneSquad-<GroupName>' CloudWatch dashboards to check that instances are starting/stopping as expected.





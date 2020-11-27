
# Step #2: 'Performance and non-regression activities' Quick-Win 

## Additional context:

Even not a recommended best-practice, a reduced set of shared environments is used to perform performance and non-regression activities.
* For performance testings, the goal is ensure that the application can sustain the load for the peak.
* For integration non-regression testings, all components are deployed but these tests are generating a fraction of the load of the Performance use-case.

These environments runs all time as the tests are queued by an orchestrator and many application configurations are tested in sequence in a continuous
way to be able to detect low occurence and rare bugs.

> ACME would like a mean to easily scale the number of EC2 resources based on the use-case (Performance or non-regression)

## Proposed improvment using CloneSquad

For this use-case, the proposal is to use an on-demand imperative scaling depending on the kind of activities. Prior to launch of a new
activity, the ACME test orchestrator will ask the CloneSquad API to scale (=starting or stopping EC2 instances) based on a pre-defined resource usage plan.

* Find a naming convention for each environment (ex: perf1, nonreg1, perf2, nonreg2 etc...)
* Deploy a CloneSquad CloudFormation template for each environment name **AND both backend and frontend layers**. So, 2 CloneSquad deployments are needed by environment. 
	- `GroupName` for the frontend: Value `<environment>-frontend`(ex: `perf1-frontend`)
	- `GroupName` for the backend: Value `<environment>-backend`(ex: `perf1-backend`)
	- See [README / Getting started](../README.md#installing--getting-started) for CloneSquad deployment instructions.
* Tag all development environment resources (EC2 resources) with their dedicated name (see previous bullet):
	- `clonesquad:group-name`: <GroupName> (ex: "perf1-frontend" end "perf1-backend")
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





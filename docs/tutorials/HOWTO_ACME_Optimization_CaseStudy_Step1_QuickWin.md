# Step #1: 'Integration/Development and Pre-production optimization' Quick-Win 

## Additional context:

ACME is using many small development environments associated to each developper using `t3` burstable instances and *Aurora for PostgreSQL* database.
ACME would like to optimize these environments by requesting the environment owners (developpers) to explicilty start them when they needed it
(for instance, when arriving at work in the morning) and that all these development environments be automatically stopped at 8PM each day. 

The application has some dependencies between Tiered layers that are not yet removed so the database needs to be started 2 minutes before the Backend instances and the Frontend instances have to be started 2 minutes after the backend ones.
When stopping a whole environment, the sequence must be the opposite.

> ACME is expecting at least a 40% cost reduction this way in AWS resources linked to Development activities.

## Proposed improvment using CloneSquad

For this "ON/OFF" like use-case, the autoscaling feature is not needed and the proposition will only use the 'static-subfleet', the API Gateway and the builtin scheduler features.

* Find a naming convention for each environment (ex: dev-user1, dev-user2, dev-user3 etc...)
* Deploy a CloneSquad CloudFormation template for each environment name (specify the environment name in the `GroupName` template variable)
	- See [README / Getting started](../../README.md#installing--getting-started) for instructions.
* Tag all development environment resources (EC2 and RDS resources) with their dedicated name:
	- `clonesquad:group-name`: <GroupName> (ex: "dev-user1")
* Tag with the additional tag `clonesquad:static-subfleet-name` depending on the layer:
	- Frontend EC2 instances: Value `frontend`
	- Backend EC2 instances: Value `backend`
	- Aurora database: Value `database`
* Locate the API Gateway URL in the CloudFormation Outputs (parameter `InteractAPIWUrl`)
* Install [awscurl](https://github.com/okigan/awscurl) on the developper desktop (or best, manage the following steps through a CI tool like RunDeck, Jenkins...)
* Write a start shell scripts to be used by developpers in the morning:

```shell
#!/bin/bash
# Start the database
awscurl -X POST -d running https://abcdefghij.execute-api.eu-west-1.amazonaws.com/v1/configuration/staticfleet.database.state
sleep 120
# Start the backend
awscurl -X POST -d running https://abcdefghij.execute-api.eu-west-1.amazonaws.com/v1/configuration/staticfleet.backend.state
sleep 120
# Start the frontend
awscurl -X POST -d running https://abcdefghij.execute-api.eu-west-1.amazonaws.com/v1/configuration/staticfleet.fronted.state
```

Configure the scheduler to stop the environment after 8PM UTC everyday:
* Create a YAML file `cronfile.yaml` like follow:
```yaml
stop-frontend-at-8PM: "cron(0 20 * * ? *),staticfleet.frontend.state=stopped"
stop-backend-at-8PM: "cron(2 20 * * ? *),staticfleet.backend.state=stopped"
stop-database-at-8PM: "cron(4 20 * * ? *),staticfleet.database.state=stopped"
```

* Upload the configuration to the Scheduler configuration:

```shell
awscurl -X POST -d @cronfile.yaml https://abcdefghij.execute-api.eu-west-1.amazonaws.com/v1/scheduler?format=yaml
```

* Look at the *'CloneSquad-environment-**'* CloudWatch dashboards to check that instances are starting/stopping as expected.
	- Notice that, if the 't3' instances were recently launched, they do not stop immediatly but enter the '[CPU Crediting](../COST_OPTIMIZATION.md#clonesquad-cpu-crediting)' mode. They will stop automatically after they accrued 30% of their maximum CPU Credit. It helps to avoid [unlimited bursting](../COST_OPTIMIZATION.md#clonesquad-cpu-crediting) fees. See the metric 'NbOfCPUCreditingInstances' on the CloudWatch dashboard to identify this situation (Note: This behavior can be disabled if not expected).

> Tip: A very similar optimization method can be applied for Pre-Production workload.


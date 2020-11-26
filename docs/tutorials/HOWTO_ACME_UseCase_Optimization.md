
# A step-by-step tutorial for a new CloneSquad user

## The 'ACME cost optimization' scenario

The ACME company migrated recently a critical application to AWS with a Rehost (aka List&Shift) approach.
The workload is deployed in a single AWS acccount with a single VPC. 

> Note: For the sake of tutorial simplicity. we consider that 
the customer did not follow AWS best practices like multi-Account strategy and network seggregation by kind of workload.

![Schema]()

The workload architecture is a mutable one and relies on lots of hardcoded dependencies (IP address, DNS names) with
limited ability to recreate the EC2 instances (aka **Pet** machines): No elasticity mecanism is used (especially, no AWS Auto-Scaling). 
The PostgreSQL database was already replatformed to *Amazon Aurora for PostgreSQL* during the migration.

ACME is planning to refactor to immutable EC2 patterns and cloud native but, 
in the mean time, there is an immediate pressure from stakeholders (management especially) to reduce cost as fast as possible before this refactoring effort.

The workload is a traditionnal 3-Tier application with Frontend and Backend sized in Production to the Peak loads that occur each day between 
7AM-9AM and 1PM-3PM; during the Weekends, there is almost neither Production nor Non-Production activity. It also happens that some peak loads 
occur from time to time out of the normal daily peak window: Most of the time, it can be anticipated few hours in advance in front of this event but sometimes it is unpredictable.

The application has different kinds of resource usage pattern:
* **Development activities**: A reduced set of resources are needed only during Business hours.
* **Integration activities**: Same as Development with the additional need for dedicated tooling instances.
* **Performance and non-regression activities**: A representative environment at scale is needed. Due to Production alike sizing and continuous delivery & testing practice of many different application configurations, ACME have to keep this environment up and running at all time.
* **Pre-Production activities**: A clone of the Production setup, needed on-demand and few hours per week. 
* **Production**: Even there is almost no traffic during the Week-end and at night, the application must be available at all time.

Currently, all resources are always on and ACME is investigating the usage of CloneSquad to perform an iterative cost optimization effort with the initial
expectation to do quickwins and show visible financial posture improvments in matter of days. 

> This tutorial will demonstrate plausible cost optimizations in step-by-step manner to achieve ACME objectives.

# Step #1: 'Optimize the Development' Quick-Win 

## Additional context:

ACME is using many small development environments associated to each developper using `t3` burstable instances and *Aurora for PostgreSQL* database.
ACME would like to optimize these environments by requesting the environment owners (developpers) to explicilty start them when they needed it
(for instance, when arriving at work in the morning) and that all these development environments be automatically stopped at 8PM each day. 

The application has some dependencies between Tiered leayes that are not yet removed so the database needs to be started 2 minutes before the Backend instances and the Frontend instances have to be started 2 minutes after the backend ones.
When stopping a whole environment, the sequence must be the opposite.

> ACME is expecting at least a 40% cost reduction this way in AWS resources linked to Development activities.

## Proposed improvment using CloneSquad

* Find a naming convention for each environment (ex: dev-user1, dev-user2, dev-user3 etc...)
* Deploy a CloneSquad CloudFormation template for each environment name (specify the environment name in the `GroupName` template variable)
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
# Start the database
awscurl -X POST -d running https://d54ss8eypc.execute-api.eu-west-1.amazonaws.com/v1/configuration/staticfleet.database.state
sleep 120
# Start the backend
awscurl -X POST -d running https://d54ss8eypc.execute-api.eu-west-1.amazonaws.com/v1/configuration/staticfleet.backend.state
sleep 120
# Start the frontend
awscurl -X POST -d running https://d54ss8eypc.execute-api.eu-west-1.amazonaws.com/v1/configuration/staticfleet.fronted.state
```

* Configure the scheduler to stop the environment after 8PM UTC every day:
	1) Create a YAML file `cronfile.yaml` like follow:
```yaml
stop-frontend-at-8PM: "cron(0 20 * * ? *),staticfleet.frontend.state=stopped"
stop-backend-at-8PM: "cron(2 20 * * ? *),staticfleet.backend.state=stopped"
stop-database-at-8PM: "cron(4 20 * * ? *),staticfleet.database.state=stopped"
```

	2) Upload the configuration to the Scheduler configuration:

```shell
awscurl -X POST -d @cronfile.yaml https://abcdefghij.execute-api.eu-west-1.amazonaws.com/v1/scheduler?format=yaml
```



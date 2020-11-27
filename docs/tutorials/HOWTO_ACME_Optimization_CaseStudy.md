
# A fictional case study of a cost optimization with CloneSquad

## The 'ACME cost optimization' scenario

ACME company migrated recently a critical application to AWS with a Rehost (aka *List & Shift*) approach.
The workload is deployed in a single AWS acccount with a single VPC. 

> Note: For the sake of tutorial simplicity. we consider that 
the customer did not follow AWS best practices like multi-Account strategy and network seggregation by kind of workloads.

![Schema]()

The workload architecture is a mutable one (EC2 instances created with CloudEndure tool) and relies on lots of hardcoded dependencies (IP address, DNS names) with
limited ability to rebuild the EC2 instances from scratch (aka **Pet** machines): No elasticity mecanism is used (especially, no AWS Auto-Scaling). 
The PostgreSQL database was already replatformed to *Amazon Aurora for PostgreSQL* during the migration.

ACME is planning to refactor to immutable EC2 patterns and cloud native ServerLess but, 
in the mean time, there is an immediate pressure from stakeholders (management especially) to reduce cost as fast as possible before this refactoring effort.

The workload is a traditionnal 3-Tier application with Frontend and Backend sized in Production to the Peak loads that occur each day between 
7AM-9AM and 1PM-3PM; during the Weekends, there is almost no activity, neither for Production nor Non-Production. It also happens that some peak loads 
occur from time to time out of the normal daily peak window: Most of the time, it can be anticipated few hours in advance in front of the event but sometimes it is unpredictable.

The application has different kinds of resource usage pattern:
* **Integration/Development activities**: A reduced set of resources are needed only during Business hours with some tooling instances.
* **Performance and non-regression activities**: A representative environment at scale is needed. Due to Production alike sizing and continuous delivery & testing practice of many different application configurations, ACME have to keep this environment up and running at all time.
* **Pre-Production activities**: A clone of the Production setup, needed on-demand and few hours per week. 
* **Production**: Even there is almost no traffic during the Week-end and at night, the application must be available at all time.

Currently, all resources are always on and ACME is investigating the usage of CloneSquad to perform an iterative cost optimization effort with the initial
expectation to do quickwins and show visible financial posture improvments in matter of days. 

> This tutorial will demonstrate plausible cost optimizations in step-by-step manner to achieve ACME objectives.


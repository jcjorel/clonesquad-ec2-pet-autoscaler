
# A step-by-step tutorial for a new CloneSquad user

## The 'ACME cost optimization' scenario

The ACME migrated recently a critical application to AWS with a Rehost(aka List&Shift) approach.
The workload is spread over 2 VPCs in a single AWS acccount.

These VPCs are:
* Production,
* Non-Production: For Integration/Pre-Prod and Development plus some shared services.

![Schema]()

The workload is a mutable and relies on lots of hardcoded dependencies (IP address, DNS names) with
limited ability to recreate the EC2 instances (aka Pet machines). The PostgreSQL database were already replatformed to Amazon Aurora for PostgreSQL
during the migration.

You are planning to refactor to immutable EC2 patterns and cloud native but, 
in the mean time, you feel immediate pressure from stakeholders (management especially) to reduce cost as fast as possible before this refactoring effort.

Your workload is a traditionnal 3-Tier application with Frontend and Backend sized in Production to the Peak loads of the day that occur each day between 
7AM-9AM and 1PM-3PM; during the Weekends, there is almost neither Production nor Non-Production activity. It also happens that some peak loads 
occur from time to time: Most of the time, it can be anticipated few hours in advance in front of this event but sometimes it is unpredictable.

The development, integration and performance activities are sharing the same Non-Production VPC. They cover different needs and cost optimization expectations:
* Development activities: A reduced set of resources are needed only during Business hours.
* Integration activities: Same as Development with the additional need for dedicated tooling instances.
* Pre-Prod/Performance activities: A representative environment at scale is a must but only needed, on-demand and few hours per week.
* Production: Even there is almost no traffic during the Week-end, the application must be available at all time.

Currently, all resources are always on and you are investigating the usage of CloneSquad to perform an iterative cost optimization effort with the initial
expectation to do quickwins and show visible financial posture improvments in matter of days. 

> PS: For the sake of simplicity, the whole scenario will sit in a single account and a limited set of VPCs but a real life deployment would
leverage a multi account strategy and associated best-practices.



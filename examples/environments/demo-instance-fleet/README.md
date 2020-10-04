
# Instance fleet demo

A demo with variants showing fleet creation with a Spot and On-Demand combination, fair instances spread over multi-AZ and,
optionnaly, with vertical scaling configuration.

	WARNING: Do not reuse AS-IS the generated template for real world projects as instances
	are customized with a cgi-bin named 'cpu' to generate CPU load for demo purposes. Embedding such
	cpu-stress-tool cgi-bin in real project by mistake could offer an easy path to DDoS for malicious people.

# Fleet generation

	Launch demo deployment from the CloneSquad DevKit!

The script file named 'deploy-test-instances.sh' generates a Cloudformation template and deploys it directly.

By default, it defines a fleet of 20 instances with 3 x t3.medium Spot instances, 4 x c5.large Spot instances and 13 x m5.large.

```shell
FLEET_SPECIFICATION=${FLEET_SPECIFICATION:-"t3.medium,Spot=True,Count=3;c5.large,Spot=True,Count=4;m5.large,Count=13"}
```


```shell
./deploy-test-instances.sh
```

Note: The script also generates some 'static subfleet' resources (4 x EC2 instances and 4 x RDS instances). There are used by the 
[demo-scheduled-events](../demo-scheduled-events/)


# Vertical scaling and 'LightHouse' instances

[Vertical scaling](../../../docs/SCALING.md#vertical-scaling) can be configured to define priorities between instance types of the fleet.

The below command line will inject the [configure-ligthhouse-instance.yaml](configure-ligthhouse-instance.yaml) policy definition
in the Configuration DynamoDB table. Please look at comments in this YAML file to understand what is the vertical scaling policy defined.

```shell
${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${GroupName}-Configuration import <configure-ligthhouse-instance.yaml
```

# Known bugs

* When existing persistent Spot instances are updated by the template.ymal Cloudformation scripts, former 
Spot request are not currectly cancelled. User need to cancel it by himself (console or API).


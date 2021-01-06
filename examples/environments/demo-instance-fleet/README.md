
# Instance fleet demo

A demo with variants showing fleet creation with a Spot and On-Demand combination, fair instances spread over multi-AZ and,
optionnaly, with vertical scaling configuration.

	WARNING: Do not reuse AS-IS the generated template for real world projects as instances
	are customized with a cgi-bin named 'cpu' to generate CPU load for demo purposes. Embedding such
	cpu-stress-tool cgi-bin in real project by mistake could offer an easy path to DDoS for malicious people.

# Cost

**WARNING: When Running the whole fleet at full size, it can cost up to 50$ per day!** When running at minimum size, the fleet cost is 
about 5$ per day.

# Fleet generation


The script file named 'deploy-test-instances.sh' generates a Cloudformation template and deploys it directly.

By default, it defines a **main autoscaled fleet** of 20 instances with *3 x t3.medium Spot instances, 4 x c5.large Spot instances, 
8 x c5.large On-Demand and 5 x c5.xlarge*.


```shell
FLEET_SPECIFICATION=${FLEET_SPECIFICATION:-"t3.medium,Spot=True,Count=3;c5.large,Spot=True,Count=4;c5.large,Count=8;c5.xlarge,Count=5"}
```


The script also generates 2 '**subfleets**'. There are used by the [demo-scheduled-events](../demo-scheduled-events/).

* `MySubfleet1`:
	* 2 x t3.micro Spot,
	* 2 x t3.micro On-Demand,
	* 1 x RDS MySQL
* `MySubfleet2`:
	* 2 x t3.micro On-Demand
* `MySubfleet3`:
	* 2 x Aurora DB

Note: You must launch demo deployment from the CloneSquad DevKit Docker container! See [instructions.](../../../docs/BUILD_RELEASE_DEBUG.md#configuring-the-devkit-to-launch-demonstrations).

```shell
./deploy-test-instances.sh
```


# Vertical scaling and 'LightHouse' instances

[Vertical scaling](../../../docs/SCALING.md#vertical-scaling) can be configured to define priorities between instance types of the fleet.

The below command line will inject the [configure-ligthhouse-instance.yaml](configure-ligthhouse-instance.yaml) policy definition
in the Configuration DynamoDB table. Please look at comments in this YAML file to understand what is the vertical scaling policy defined.

```shell
${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${GroupName}-Configuration import <configure-ligthhouse-instance.yaml
```

# Known bugs

* When existing persistent Spot instances are updated by the template.yaml Cloudformation scripts, former 
Spot request are not currectly cancelled (CloudFormation issue?). User need to cancel it by himself (console or API).


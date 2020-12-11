
# Cost optimization


## CloneSquad CPU Crediting

CloneSquad implements a dedicated strategy to manage burstable instances and avoid unexpected costs 
linked to the [unlimited bursting](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode.html).

> Tip: The 'CPU Crediting' feature can be disabled with [`ec2.schedule.burstable_instance.max_cpu_crediting_instances`](../CONFIGURATION_REFERENCE.md#ec2scheduleburstable_instancemax_cpu_crediting_instances) configuration key set to `0`.

The `CPUCreditBalance` metric of each burstable instances under management is monitored and they are marked 'unhealthy' 
when they exhaust their `CPUCreditBalance`. (This behavior can be modified with [`ec2.schedule.burstable_instance.max_cpu_credit_instance_issues`](../CONFIGURATION_REFERENCE.md#ec2scheduleburstable_instancemax_cpu_credit_instance_issues))

When marked 'unhealthy', a new instance will be automatically started to allow its replacement (The started instance will be selected by the autoscaler algorithm
without taking into account that a burstable is replaced). After a period of time, burstable instances with exhausted 'CPUCreditBalance` will
be marked as 'draining', will be unsubscribed from all targetgroups and so, have their CPU going down to zero. They will remain in this state for a long time: This is the 'CPU Crediting' mode.

While in 'CPU Crediting' mode, the `CPUCreditBalance` is monitored until it reachs 30% of daily accruable credits and then the instance is stopped.

By default, no more than 50% of a CloneSquad fleet can be, at the same time, in CPU Crediting mode.

If you see lots of instances in 'CPU Crediting`mode, it is recommended to increase the instance type of your instances.

## CPU Credit preservation

Another cost optimization strategy is implemented that starts burstable instances stopped for more than 6 days and 12 hours to avoid [losing accrued credits](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html#accrued-CPU-credits-life-span).

> Tip: The 'CPU Credit preservation' feature can be disabled with [`ec2.schedule.burstable_instance.preserve_accrued_cpu_credit`](../CONFIGURATION_REFERENCE.md#ec2scheduleburstable_instancepreserve_accrued_cpu_credit)` configuration key set `0`.

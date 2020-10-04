
# Demo instance bouncing

This is a very simple demonstration about how to configure Instance bouncing (disabled by default).

It defines an instance period of 2 hours and a half that is fast. It means that as soon as an instance is running
for more than 2hr 30, it will be placed in a queue to be 'bounced'.

The bouncing algorithm starts a fresh instance and will stop the bounced one few minutes later.

```shell
${CLONESQUAD_DIR}/tools/cs-kvtable CloneSquad-${GroupName}-Configuration import <configure_fast_bouncing.yaml
```

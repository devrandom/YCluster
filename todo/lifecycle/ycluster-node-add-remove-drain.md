# ycluster node add/remove/drain

`ycluster node {add,remove,drain}` CLI wrappers around the replace-a-dead
node and graceful-decommission runbooks so it's not a manual
ssh+sudo dance during an outage.

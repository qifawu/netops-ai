# OSPF neighbor states and what a stuck state usually means

These notes are original, written for the netops-ai sample knowledge base. They summarize widely documented OSPF behavior; check your vendor documentation for details.

## The state sequence

An OSPF adjacency normally walks through Down, Init, 2-Way, ExStart, Exchange, Loading and Full. On broadcast networks, routers that are not the DR or BDR stay in 2-Way with each other: that is normal, not a fault.

## Where it gets stuck

- **Down after "Dead timer expired".** No Hello was received within the dead interval. The neighbor stopped sending, or Hellos are not arriving. Check the neighbor's interface state and whether the path between the two routers drops multicast 224.0.0.5.
- **Stuck in Init.** Hellos are received but our router ID is not listed in the neighbor's Hellos: the neighbor does not hear us. Typical causes are a one-way link, an access list, or a mismatched network type.
- **Stuck in 2-Way** between two routers on a point-to-point link. A mismatched network type (one side broadcast, the other point-to-point) is the usual reason.
- **Stuck in ExStart or Exchange.** MTU mismatch is the classic cause: the database description packets do not fit. Compare `show ip ospf interface` on both sides.
- **Flapping between Full and Down.** Look for physical errors, duplex mismatch, or a congested link that drops Hellos.

## Parameters that must match

Area ID, Hello and Dead intervals, network type, authentication, and the subnet and mask on the link. A difference in any of them keeps the adjacency from forming.

## Useful read-only commands

`show ip ospf neighbor`, `show ip ospf interface`, `show ip interface brief`, `show logging | include OSPF`.

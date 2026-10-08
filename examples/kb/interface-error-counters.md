# Reading interface error counters

These notes are original, written for the netops-ai sample knowledge base. They summarize widely documented behavior; check your vendor documentation for details.

## What the counters mean

- **CRC / input errors.** Frames arrived damaged. Usually a physical-layer problem: a bad cable or connector, a failing optic, electrical noise, or a speed/duplex mismatch.
- **Runts and giants.** Frames shorter or longer than allowed. Runts often come with collisions or duplex mismatch; giants with MTU mismatches on the path.
- **Collisions and late collisions.** Normal only on half-duplex links. On a link that should be full duplex they point at a duplex mismatch: one side negotiated half duplex, the other was forced to full.
- **Output drops.** The output queue overflowed: congestion, not a damaged link.
- **Interface resets and carrier transitions.** The link bounced. Look at both ends and the physical path.

## Cumulative counters need a time dimension

These counters only ever grow until cleared. A large number says the problem happened at some point, not that it is happening now. Compare two readings, or look at the monitoring history of the same counter, before concluding the link is still failing.

## Duplex mismatch checklist

1. Read speed and duplex on both ends (`show interfaces <name>`, `show interfaces status`).
2. If one end is hard-set and the other auto-negotiates, the auto side falls back to half duplex. Set both ends the same way.
3. After correcting, watch the counters stop growing rather than expecting them to return to zero.

## Useful read-only commands

`show interfaces <name>`, `show interfaces status`, `show interfaces counters errors`, `show logging | include <name>`.

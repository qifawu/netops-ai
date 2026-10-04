# Interface Messages

## LINK-3-UPDOWN message

%LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to down

Explanation: This sample message says the physical interface state changed. In a lab it is often caused by a cable pull, disabled peer port, optic issue, or remote device power loss.

Recommended Action: Check the local interface counters, peer port state, recent maintenance records, and whether the neighbor also reports a link transition. Do not assume the root cause from this message alone.

## Generic interface note

A single transition can be harmless during planned work. Repeated transitions suggest a physical layer or peer-side stability problem.

"""How many more runner VMs the host can take without squeezing its other guests.

The configured budget caps what the runners may use at all; the host reserve protects everything else
on the hypervisor. Memory that other guests are promised counts as used even while they do not touch it or
are stopped, because a guest may claim it at any moment and the runners must give way, not the other guests.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HostMemory:
    """The host's physical memory and what its guests are promised, whether they run or not."""

    total_mb: int
    committed_mb: int


def vms_that_fit(host: HostMemory, *, reserve_mb: int, budget_left_mb: int, vm_mb: int) -> int:
    """The number of additional VMs of `vm_mb` that fit both the budget and the host reserve."""
    if vm_mb <= 0:
        raise ValueError("vm_mb must be positive")
    host_left = host.total_mb - host.committed_mb - reserve_mb
    return max(0, min(host_left, budget_left_mb) // vm_mb)

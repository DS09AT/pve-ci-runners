"""The firewall file of a runner VM, the single definition of what a job may reach.

Inbound traffic is dropped, the address filter pins the VM to its slot address, and egress to every
private, link-local, multicast and IPv6 destination is dropped, so a job reaches the internet but neither
the hypervisor nor other guests. SSH egress is limited to the configured destinations because the
runners share the host's outbound address with whatever trusts that address. The rules match first to
last: explicit extra rules come first, then the denials, so no SSH destination can reopen a private one.
"""

from pve_ci_runners.config import PRIVATE_RANGES, FirewallConfig, Slot

_DENIED_DESTINATIONS = (
    *((cidr, "private and shared networks") for cidr in PRIVATE_RANGES),
    ("169.254.0.0/16", "link-local and cloud metadata"),
    ("224.0.0.0/3", "multicast and reserved"),
    ("::/0", "no IPv6 egress"),
)


def render(slot: Slot, config: FirewallConfig) -> str:
    """The `<vmid>.fw` content for this slot."""
    lines = [
        "[OPTIONS]",
        "enable: 1",
        "policy_in: DROP",
        "policy_out: ACCEPT",
        "macfilter: 1",
        "ipfilter: 1",
        "dhcp: 0",
        "ndp: 0",
        "radv: 0",
        "log_level_in: nolog",
        "log_level_out: nolog",
        "",
        "[IPSET ipfilter-net0]",
        str(slot.address),
        "",
        "[RULES]",
        *config.extra_rules,
        *(f"OUT DROP -dest {cidr} # {why}" for cidr, why in _DENIED_DESTINATIONS),
        f"OUT ACCEPT -dest {','.join(config.ssh_destinations)} -p tcp -dport 22 # git over SSH",
        "OUT DROP -p tcp -dport 22 # no other SSH egress",
        "OUT DROP -p tcp -dport 25 # no outbound mail",
        "",
    ]
    return "\n".join(lines)

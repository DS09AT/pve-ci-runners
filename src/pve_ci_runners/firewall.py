"""The firewall file of a runner VM, the single definition of what a job may reach.

Inbound traffic is dropped, the address filter pins the VM to its slot address, and egress is dropped to
every private, link-local, multicast and IPv6 destination, to the configured denied destinations and to
every address of the hypervisor and the networks those addresses sit on, so a job reaches the internet but
neither the hypervisor, its neighbours on any of its networks nor other guests. SSH egress is limited to
the configured destinations because the runners share the host's outbound address with whatever trusts
that address. The rules match first to last: the denials come first, then the explicit extra rules, then
the SSH and mail rules, so no extra rule and no SSH destination can reopen a denied destination.
"""

import ipaddress
from collections.abc import Collection, Sequence

from pve_ci_runners.config import PRIVATE_RANGES, FirewallConfig, Slot

DENY_IPSET = "deny"
_PROBE_SAMPLE = 8
_DENIED_DESTINATIONS = (
    *((cidr, "private and shared networks") for cidr in PRIVATE_RANGES),
    ("169.254.0.0/16", "link-local and cloud metadata"),
    ("224.0.0.0/3", "multicast and reserved"),
    ("::/0", "no IPv6 egress"),
)


def deny_set(config: FirewallConfig, host_networks: Sequence[str]) -> tuple[str, ...]:
    """The configured denied destinations, then the hypervisor's addresses and networks, each once."""
    return tuple(dict.fromkeys((*config.deny_destinations, *host_networks)))


def probe_sample(denied: Sequence[str], probed: Collection[str]) -> tuple[str, ...]:
    """One IPv4 address in each of the first denied networks that the probe does not try already.

    IPv6 egress is dropped as a whole and the probe checks that the guest has no IPv6 at all.
    """
    sample: list[str] = []
    for network in map(ipaddress.ip_network, denied):
        address = str(next(iter(network.hosts())))
        if isinstance(network, ipaddress.IPv4Network) and address not in {*probed, *sample}:
            sample.append(address)
    return tuple(sample[:_PROBE_SAMPLE])


def render(slot: Slot, config: FirewallConfig, host_networks: Sequence[str]) -> str:
    """The `<vmid>.fw` content for this slot; `host_networks` comes from `Hypervisor.host_networks`."""
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
        f"[IPSET {DENY_IPSET}]",
        *deny_set(config, host_networks),
        "",
        "[RULES]",
        f"OUT DROP -dest +{DENY_IPSET} # denied destinations and the hypervisor's networks",
        *(f"OUT DROP -dest {cidr} # {why}" for cidr, why in _DENIED_DESTINATIONS),
        *config.extra_rules,
        f"OUT ACCEPT -dest {','.join(config.ssh_destinations)} -p tcp -dport 22 # git over SSH",
        "OUT DROP -p tcp -dport 22 # no other SSH egress",
        "OUT DROP -p tcp -dport 25 # no outbound mail",
        "",
    ]
    return "\n".join(lines)

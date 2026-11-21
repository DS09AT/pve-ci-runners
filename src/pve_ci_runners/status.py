"""What every slot does right now, read from the host alone."""

import json
from dataclasses import dataclass

from pve_ci_runners.config import Config
from pve_ci_runners.drain import draining
from pve_ci_runners.ports import Hypervisor
from pve_ci_runners.pve import TAG
from pve_ci_runners.state import decode
from pve_ci_runners.templates import current_template


@dataclass(frozen=True)
class SlotStatus:
    """One slot as an operator sees it."""

    vmid: int
    phase: str
    profile: str = ""
    repository: str = ""
    since: int = 0


@dataclass(frozen=True)
class Status:
    """The whole host as an operator sees it."""

    draining: bool
    template: int | None
    template_built_at: int | None
    slots: tuple[SlotStatus, ...]

    def to_json(self) -> str:
        """A stable machine-readable form."""
        return json.dumps(
            {
                "format": 1,
                "draining": self.draining,
                "template": self.template,
                "template_built_at": self.template_built_at,
                "slots": [s.__dict__ for s in self.slots],
            },
            sort_keys=True,
        )

    def to_text(self) -> str:
        """A short table for a terminal."""
        lines = [
            f"draining: {'yes' if self.draining else 'no'}",
            f"template: {self.template if self.template is not None else 'none finished'}",
        ]
        lines += [f"{s.vmid:>6}  {s.phase:<9} {s.profile:<12} {s.repository}" for s in self.slots]
        return "\n".join(lines)


def collect(config: Config, hypervisor: Hypervisor) -> Status:
    """Reads every slot and the template pair."""
    infos = hypervisor.vms()

    def description(vmid: int) -> str:
        return str(hypervisor.config(vmid).get("description", ""))

    slots: list[SlotStatus] = []
    for slot in config.slots:
        info = infos.get(slot.vmid)
        if info is None:
            slots.append(SlotStatus(slot.vmid, "free"))
            continue
        state = decode(description(slot.vmid), running=info.running) if TAG in info.tags else None
        if state is None:
            slots.append(SlotStatus(slot.vmid, "foreign" if TAG not in info.tags else "invalid"))
            continue
        slots.append(SlotStatus(slot.vmid, state.phase, state.profile, state.repository or "", state.since))
    template = current_template(config.template.vmids, infos, description)
    return Status(
        draining=draining(config.daemon.state_dir),
        template=template[0] if template else None,
        template_built_at=template[1].built_at if template else None,
        slots=tuple(slots),
    )

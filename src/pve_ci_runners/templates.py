"""Which template new runner VMs are cloned from.

Two template VMIDs take turns: a build writes the one not in use, and marks it complete in its
description only at the very end, so an interrupted build is never cloned.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from pve_ci_runners.pve import VmInfo

MARKER_KIND = "pve-ci-runners-template"


@dataclass(frozen=True)
class TemplateMarker:
    """What a finished build records on its template."""

    built_at: int
    runner_version: str

    def encode(self) -> str:
        """The description text."""
        return json.dumps(
            {"kind": MARKER_KIND, "built_at": self.built_at, "runner_version": self.runner_version},
            separators=(",", ":"),
            sort_keys=True,
        )


def decode_marker(description: str) -> TemplateMarker | None:
    """The marker in a template's description, or None when it is not a finished build."""
    try:
        data: object = json.loads(description)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = cast("dict[str, object]", data)
    built_at, version = fields.get("built_at"), fields.get("runner_version")
    if fields.get("kind") != MARKER_KIND or not isinstance(built_at, int) or not isinstance(version, str):
        return None
    return TemplateMarker(built_at=built_at, runner_version=version)


def current_template(
    vmids: tuple[int, int], vms: Mapping[int, VmInfo], description: Callable[[int], str]
) -> tuple[int, TemplateMarker] | None:
    """The newest finished template among the configured pair."""
    found = [
        (vmid, marker)
        for vmid in vmids
        if vmid in vms and vms[vmid].template
        if (marker := decode_marker(description(vmid))) is not None
    ]
    return max(found, key=lambda item: item[1].built_at) if found else None

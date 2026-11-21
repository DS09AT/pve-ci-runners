"""Shared fixtures: a valid configuration that every test varies from."""

import copy
from typing import Any

import pytest

from pve_ci_runners.config import Config, parse

BASE: dict[str, Any] = {
    "github": {
        "owner": "example-org",
        "app_client_id": "Iv23liEXAMPLE",
        "app_key_file": "/etc/pve-ci-runners/app.pem",
    },
    "capacity": {"memory_mb": 20480, "host_reserve_mb": 4096},
    "network": {"bridge": "vmbr1", "gateway": "192.168.100.1", "prefix": 24, "dns": ["192.0.2.53"]},
    "slot": [{"vmid": 121, "address": "192.168.100.21"}, {"vmid": 122, "address": "192.168.100.22"}],
    "profile": [
        {
            "name": "default",
            "match": "pve-ci",
            "labels": ["self-hosted", "linux", "x64", "pve-ci"],
            "cores": 4,
            "memory_mb": 5120,
            "warm": 1,
            "max": 2,
            "qm_options": {"cpuunits": 100},
        }
    ],
    "cache": {"backend": "zfs", "dataset": "rpool/ci-cache"},
    "template": {
        "vmids": [9201, 9202],
        "storage": "local-zfs",
        "image_url": "https://images.example.org/noble-server-cloudimg-amd64.img",
        "image_sha256": "0" * 64,
        "address": "192.168.100.20",
    },
    "overflow": {
        "variable": "CI_RUNS_ON",
        "hosted_value": '"ubuntu-latest"',
        "repositories": ["example-org/app"],
    },
}


def config_data(**changes: Any) -> dict[str, Any]:
    """A deep copy of the base configuration with top-level sections replaced."""
    data = copy.deepcopy(BASE)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = copy.deepcopy(value)
    return data


@pytest.fixture
def config() -> Config:
    """The base configuration, parsed."""
    return parse(config_data())

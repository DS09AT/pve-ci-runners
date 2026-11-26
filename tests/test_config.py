import copy
import os
from pathlib import Path

import pytest

from pve_ci_runners.config import DEFAULT_MOUNTS, MAX_CACHE_DATASET, check_secret_file, load, parse
from pve_ci_runners.errors import ConfigError
from tests.conftest import BASE, config_data


def test_the_base_configuration_parses_with_defaults() -> None:
    config = parse(config_data())

    assert config.profiles[0].qm_options == (("cpuunits", "100"),)
    assert config.cache.mounts == DEFAULT_MOUNTS
    assert config.overflow is not None
    assert config.overflow.self_value == '["self-hosted","pve-ci"]'
    assert config.overflow.reroute_after_seconds == 120
    assert config.overflow.reroute_discard_seconds == 0, "cutting jobs short is opt-in"
    assert not config.overflow.capacity_variable
    assert config.daemon.poll_seconds == 15
    assert config.daemon.max_job_seconds == 23400
    assert str(config.network.network) == "192.168.100.0/24"


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("github", "tokn", "x", "unknown key"),
        ("github", "owner", "-bad-", "does not match"),
        ("github", "api_url", "http://api.github.com", "does not match"),
        ("network", "prefix", 31, "between 8 and 30"),
        ("network", "dns", [], "between 1 and 3"),
        ("capacity", "memory_mb", "lots", "must be an integer"),
        ("capacity", "memory_mb", True, "must be an integer"),
    ],
)
def test_invalid_values_name_the_key(section: str, key: str, value: object, message: str) -> None:
    data = config_data()
    data[section][key] = value

    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_a_missing_section_is_an_error() -> None:
    with pytest.raises(ConfigError, match="network: is required"):
        parse(config_data(network=None))


def test_an_unknown_top_level_section_is_an_error() -> None:
    data = config_data()
    data["runners"] = {}

    with pytest.raises(ConfigError, match="unknown key"):
        parse(data)


@pytest.mark.parametrize("option", ["args", "hookscript", "net0", "description", "scsi0"])
def test_profiles_cannot_set_options_the_controller_owns_or_that_run_code(option: str) -> None:
    data = config_data()
    data["profile"][0]["qm_options"] = {option: "x"}

    with pytest.raises(ConfigError, match="not an allowed key"):
        parse(data)


def test_option_values_cannot_smuggle_further_options() -> None:
    data = config_data()
    data["profile"][0]["qm_options"] = {"cpu": "host,flags=+aes"}

    with pytest.raises(ConfigError, match="single value"):
        parse(data)


def test_slots_must_be_distinct_host_addresses_off_the_gateway() -> None:
    for address, message in (
        ("192.168.100.22", "own address"),
        ("192.168.100.1", "is the gateway"),
        ("192.168.101.5", "not a host address"),
        ("192.168.100.255", "not a host address"),
    ):
        data = config_data()
        data["slot"][0]["address"] = address
        with pytest.raises(ConfigError, match=message):
            parse(data)


@pytest.mark.parametrize(
    ("gateway", "prefix"), [("203.0.113.1", 24), ("172.15.0.1", 16), ("172.16.0.1", 11), ("169.254.0.1", 16)]
)
def test_the_bridge_network_must_lie_inside_a_denied_private_range(gateway: str, prefix: int) -> None:
    data = config_data()
    data["network"] |= {"gateway": gateway, "prefix": prefix}

    with pytest.raises(ConfigError, match=r"network\.gateway: the bridge network must lie inside"):
        parse(data)


@pytest.mark.parametrize(
    ("daemon", "message"),
    [
        ({"max_job_seconds": 3599}, "between 3600 and 604800"),
        ({"max_job_seconds": 604801}, "between 3600 and 604800"),
        ({"max_job_seconds": 7200, "idle_timeout_seconds": 7200}, "must exceed idle_timeout_seconds"),
        ({"max_job_secs": 7200}, "unknown key"),
    ],
)
def test_the_job_lifetime_is_bounded_and_longer_than_the_idle_timeout(
    daemon: dict[str, int], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(config_data(daemon=daemon))


def test_slot_and_template_vmids_must_differ() -> None:
    data = config_data()
    data["template"]["vmids"] = [121, 9202]

    with pytest.raises(ConfigError, match="own VMID"):
        parse(data)


def test_profiles_cannot_promise_more_than_the_slots_or_the_memory_budget() -> None:
    data = config_data()
    data["profile"][0]["max"] = 3
    with pytest.raises(ConfigError, match="number of slots"):
        parse(data)

    data = config_data(capacity={"memory_mb": 8192})
    with pytest.raises(ConfigError, match="memory_mb"):
        parse(data)


def test_profile_match_labels_are_unique_and_exclusive() -> None:
    second = copy.deepcopy(BASE["profile"][0]) | {"name": "large", "max": 0}
    data = config_data(profile=[BASE["profile"][0], second | {"max": 1}])
    data["profile"][0]["max"] = 1
    with pytest.raises(ConfigError, match="unique"):
        parse(data)

    second = second | {"match": "pve-ci-large", "labels": ["self-hosted", "pve-ci-large", "pve-ci"], "max": 1}
    data = config_data(profile=[BASE["profile"][0] | {"max": 1}, second])
    with pytest.raises(ConfigError, match="another profile's match label"):
        parse(data)


def test_labels_must_contain_self_hosted_and_the_match_label() -> None:
    data = config_data()
    data["profile"][0]["labels"] = ["linux", "pve-ci"]

    with pytest.raises(ConfigError, match="self-hosted"):
        parse(data)


@pytest.mark.parametrize("value", ["ubuntu-latest", "[]", "[1]", '""'])
def test_overflow_values_must_be_runs_on_json(value: str) -> None:
    data = config_data()
    data["overflow"]["hosted_value"] = value

    with pytest.raises(ConfigError, match="JSON"):
        parse(data)


@pytest.mark.parametrize("key", ["reroute_after_seconds", "reroute_discard_seconds"])
@pytest.mark.parametrize(
    ("value", "valid"), [(0, True), (86400, True), (-1, False), (86401, False), ("2m", False)]
)
def test_the_reroute_times_are_checked(key: str, value: object, valid: bool) -> None:
    data = config_data()
    data["overflow"][key] = value

    if valid:
        overflow = parse(data).overflow
        assert overflow is not None and getattr(overflow, key) == value
    else:
        with pytest.raises(ConfigError, match=key):
            parse(data)


@pytest.mark.parametrize(
    ("value", "message"),
    [("kb_free_slots", "does not match"), ("1SLOTS", "does not match"), ("CI_RUNS_ON", "must differ")],
)
def test_the_capacity_variable_is_a_valid_name_other_than_the_overflow_variable(
    value: str, message: str
) -> None:
    data = config_data()
    data["overflow"]["capacity_variable"] = value

    with pytest.raises(ConfigError, match=f"capacity_variable: .*{message}"):
        parse(data)


def test_a_capacity_variable_is_accepted() -> None:
    data = config_data()
    data["overflow"]["capacity_variable"] = "KB_FREE_SLOTS"

    overflow = parse(data).overflow

    assert overflow is not None and overflow.capacity_variable == "KB_FREE_SLOTS"


def test_overflow_names_an_existing_profile() -> None:
    data = config_data()
    data["overflow"]["profile"] = "large"

    with pytest.raises(ConfigError, match="names no profile"):
        parse(data)


def test_cache_mounts_and_the_none_backend() -> None:
    data = config_data(cache={"backend": "none", "mount": [{"path": "/home/runner/.cache", "dir": "cache"}]})
    config = parse(data)

    assert config.cache.backend == "none"
    assert [str(m.path) for m in config.cache.mounts] == ["/home/runner/.cache"]
    assert config.cache.mounts[0].owner == "runner"

    data = config_data(cache={"backend": "zfs"})
    with pytest.raises(ConfigError, match="dataset"):
        parse(data)


@pytest.mark.parametrize("path", ["relative/path", "/home/runner/../etc", "/home/runner/"])
def test_paths_must_be_absolute_and_normalized(path: str) -> None:
    data = config_data(cache={"backend": "none", "mount": [{"path": path, "dir": "x"}]})

    with pytest.raises(ConfigError, match="absolute path"):
        parse(data)


def test_firewall_destinations_and_extra_rules_are_validated() -> None:
    data = config_data(firewall={"ssh_destinations": ["not-a-network"]})
    with pytest.raises(ConfigError, match="not an IPv4 network"):
        parse(data)

    data = config_data(firewall={"extra_rules": ["OUT ACCEPT -dest 10.0.0.5 -p tcp -dport 443\n[OPTIONS]"]})
    with pytest.raises(ConfigError, match="does not match"):
        parse(data)

    data = config_data(firewall={"extra_rules": ["OUT ACCEPT -dest 10.0.0.5 -p tcp -dport 443 # registry"]})
    assert parse(data).firewall.extra_rules == ("OUT ACCEPT -dest 10.0.0.5 -p tcp -dport 443 # registry",)


def test_deny_destinations_are_ipv4_or_ipv6_networks_in_normal_form_each_once() -> None:
    assert parse(config_data()).firewall.deny_destinations == ()
    data = config_data(
        firewall={"deny_destinations": ["198.51.100.0/24", "2001:DB8::/32", "2001:db8::/32", "192.0.2.7"]}
    )
    assert parse(data).firewall.deny_destinations == ("198.51.100.0/24", "2001:db8::/32", "192.0.2.7/32")

    for bad in ("198.51.100.1/24", "not-a-network", "2001:db8::/129"):
        with pytest.raises(ConfigError, match="not an IPv4 or IPv6 network"):
            parse(config_data(firewall={"deny_destinations": [bad]}))
    with pytest.raises(ConfigError, match=r"lists '198\.51\.100\.0/24' twice"):
        parse(config_data(firewall={"deny_destinations": ["198.51.100.0/24", "198.51.100.0/24"]}))


def test_the_nic_rate_is_optional_and_positive() -> None:
    network = dict(config_data()["network"])
    assert parse(config_data(network=network)).network.nic == "virtio,bridge=vmbr1,firewall=1"
    assert parse(config_data(network={**network, "rate": 50, "vlan": 7})).network.nic == (
        "virtio,bridge=vmbr1,firewall=1,tag=7,rate=50"
    )
    for bad in (0, -1, 1.5, "50"):
        with pytest.raises(ConfigError, match=r"network\.rate"):
            parse(config_data(network={**network, "rate": bad}))


def test_template_needs_exactly_two_distinct_vmids() -> None:
    for vmids in ([9201], [9201, 9201], [9201, "9202"]):
        data = config_data(template={"vmids": vmids, "storage": "local-zfs"})
        with pytest.raises(ConfigError, match="vmids"):
            parse(data)


def test_load_reports_unreadable_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be read"):
        load(tmp_path / "missing.toml")

    broken = tmp_path / "broken.toml"
    broken.write_text("[github\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(broken)


def test_load_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[github]
owner = "example-org"
app_client_id = "Iv23liEXAMPLE"
[capacity]
memory_mb = 8192
[network]
bridge = "vmbr1"
gateway = "192.168.100.1"
prefix = 24
dns = ["192.0.2.53"]
[[slot]]
vmid = 121
address = "192.168.100.21"
[[profile]]
name = "default"
match = "pve-ci"
labels = ["self-hosted", "pve-ci"]
cores = 2
memory_mb = 4096
max = 1
[cache]
backend = "none"
[template]
vmids = [9201, 9202]
storage = "local-zfs"
image_url = "https://images.example.org/noble-server-cloudimg-amd64.img"
image_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
address = "192.168.100.20"
""",
        encoding="utf-8",
    )

    assert load(path).profiles[0].warm == 1


def test_secret_files_must_be_private_regular_files(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text("x", encoding="utf-8")
    key.chmod(0o644)
    with pytest.raises(ConfigError, match="too open"):
        check_secret_file(key)

    key.chmod(0o600)
    check_secret_file(key)

    with pytest.raises(ConfigError, match="regular file"):
        check_secret_file(tmp_path)
    with pytest.raises(ConfigError, match="cannot be read"):
        check_secret_file(tmp_path / "missing.pem")
    assert os.geteuid() == key.stat().st_uid


def test_the_cache_dataset_leaves_room_for_the_longest_derived_zfs_name() -> None:
    parse(config_data(cache={"backend": "zfs", "dataset": "p/" + "d" * (MAX_CACHE_DATASET - 2)}))
    with pytest.raises(ConfigError, match="at most"):
        parse(config_data(cache={"backend": "zfs", "dataset": "p/" + "d" * (MAX_CACHE_DATASET - 1)}))


def test_the_example_configuration_is_valid_and_the_readme_shows_it_unchanged() -> None:
    example = Path("packaging/config.example.toml")

    assert load(example).template.vmids == (9201, 9202)
    assert example.read_text(encoding="utf-8") in Path("README.md").read_text(encoding="utf-8")


def test_a_secret_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "app.pem"
    key.write_text("x", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: key.stat().st_uid + 1)

    with pytest.raises(ConfigError, match="must be owned by the user running the controller"):
        check_secret_file(key)


def test_disk_limits_are_positive_integers() -> None:
    data = config_data()
    data["profile"][0]["disk_limits"] = {"mbps_wr": 200, "iops_rd": 500}
    assert parse(data).profiles[0].disk_limits == (("iops_rd", 500), ("mbps_wr", 200))

    for value in (0, "fast"):
        data["profile"][0]["disk_limits"] = {"mbps_wr": value}
        with pytest.raises(ConfigError, match=r"disk_limits\.mbps_wr: must be a positive integer"):
            parse(data)


def test_template_versions_override_the_defaults_and_must_look_like_versions() -> None:
    data = config_data()
    data["template"]["versions"] = {"node": "24.1.0", "dotnet": "LTS"}
    versions = dict(parse(data).template.versions)
    assert (versions["node"], versions["dotnet"], versions["go"]) == ("24.1.0", "LTS", "1.27.4")

    for value in ("latest", 24):
        data["template"]["versions"] = {"node": value}
        with pytest.raises(ConfigError, match=r"node: .* is not a version"):
            parse(data)


def test_template_scripts_cannot_climb_out_of_their_directory() -> None:
    data = config_data()
    data["template"]["scripts"] = ["/etc/pve-ci-runners/../shadow"]

    with pytest.raises(ConfigError, match=r"must not contain '\.\.'"):
        parse(data)


def test_the_template_needs_an_address_of_its_own() -> None:
    data = config_data()
    data["template"]["address"] = data["slot"][0]["address"]

    with pytest.raises(ConfigError, match=r"template\.address: is a slot's address"):
        parse(data)


def test_the_uv_cache_is_cached_where_the_runner_points_uv() -> None:
    uv = next(m for m in DEFAULT_MOUNTS if m.directory == "uv")
    prepare = (Path(__file__).parent.parent / "src/pve_ci_runners/guest/prepare.sh").read_text(
        encoding="utf-8"
    )

    assert f"Environment=UV_CACHE_DIR={uv.path}\n" in prepare, (
        "setup-uv would fall back to a per-job temp dir"
    )


def test_disk_reserves_and_the_health_threshold_have_safe_defaults() -> None:
    config = parse(config_data())

    assert config.capacity.disk_reserve_gb == 10
    assert config.capacity.disk_reserve_bytes == 10 * 2**30
    assert config.cache.free_reserve_gb == 30, "one reserve per slot and one for a template build"
    assert config.daemon.unhealthy_checks == 3
    assert config.template.disk_gb == 40


def test_the_free_reserve_follows_the_disk_reserve_unless_it_is_set() -> None:
    data = config_data()
    data["capacity"]["disk_reserve_gb"] = 4
    assert parse(data).cache.free_reserve_gb == 12
    data["cache"]["free_reserve_gb"] = 0
    assert parse(data).cache.free_reserve_gb == 0


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("capacity", "disk_reserve_gb", 0),
        ("cache", "free_reserve_gb", -1),
        ("daemon", "unhealthy_checks", 0),
        ("daemon", "unhealthy_checks", 101),
    ],
)
def test_disk_reserves_and_the_health_threshold_are_bounded(section: str, key: str, value: int) -> None:
    data = config_data()
    data.setdefault(section, {})[key] = value

    with pytest.raises(ConfigError, match="must be between"):
        parse(data)


def test_a_large_thin_template_disk_is_accepted() -> None:
    data = config_data()
    data["template"]["disk_gb"] = 80

    assert parse(data).template.disk_gb == 80


def test_the_assign_timeout_defaults_to_ten_minutes_and_stays_below_the_job_lifetime() -> None:
    assert parse(config_data()).daemon.assign_timeout_seconds == 600
    data = config_data(daemon={"assign_timeout_seconds": 23400})

    with pytest.raises(ConfigError, match="assign_timeout_seconds"):
        parse(data)


def overflow_data(slots: int, **overflow: object) -> dict[str, object]:
    """A pool of `slots` slots, all of them held by one profile, without the base overflow thresholds."""
    data = config_data(capacity={"memory_mb": 5120 * slots, "host_reserve_mb": 4096})
    data["slot"] = [{"vmid": 121 + i, "address": f"192.168.100.{21 + i}"} for i in range(slots)]
    data["profile"][0]["max"] = slots
    for key in ("on_free_slots", "off_free_slots", "off_seconds"):
        del data["overflow"][key]
    data["overflow"] |= overflow
    return data


@pytest.mark.parametrize(("slots", "on", "off"), [(5, 1, 3), (2, 1, 2), (1, 0, 1)])
def test_overflow_free_slot_defaults_fit_the_profile(slots: int, on: int, off: int) -> None:
    overflow = parse(overflow_data(slots)).overflow

    assert overflow is not None
    assert (overflow.on_free_slots, overflow.off_free_slots, overflow.off_seconds) == (on, off, 300)


@pytest.mark.parametrize(
    ("overflow", "message"),
    [
        ({"on_free_slots": 3, "off_free_slots": 3}, "off_free_slots: must exceed on_free_slots"),
        ({"on_free_slots": 4}, "off_free_slots: must exceed on_free_slots"),
        ({"off_free_slots": 6}, "off_free_slots: must be between 1 and 5"),
        ({"on_free_slots": 6, "off_free_slots": 5}, "on_free_slots: must be between -1 and 5"),
        ({"on_free_slots": -2}, "on_free_slots: must be between -1 and 5"),
        ({"on_backlog": -1}, "on_backlog: must be between 0 and"),
        ({"off_free_slots": 0, "on_free_slots": 0}, "off_free_slots: must be between 1 and 5"),
    ],
)
def test_overflow_free_slot_thresholds_are_checked(overflow: dict[str, int], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(overflow_data(5, **overflow))

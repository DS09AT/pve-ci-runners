"""The configuration file: its schema, its defaults and every cross-field rule.

This is the trust boundary for operator input. Everything downstream receives frozen, validated objects.
"""

import ipaddress
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pve_ci_runners.cache import MAX_KEY_LENGTH
from pve_ci_runners.config_reader import Section
from pve_ci_runners.errors import ConfigError

DEFAULT_PATH = Path("/etc/pve-ci-runners/config.toml")

LABEL = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
NAME = r"[a-z0-9][a-z0-9-]{0,30}"
OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
REPOSITORY = OWNER + r"/[A-Za-z0-9._-]{1,100}"
ZFS_DATASET = r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)+"
SIZE = r"[1-9][0-9]{0,5}[MG]"
VARIABLE = r"[A-Z_][A-Z0-9_]{0,99}"

# VM options a profile may set. Anything else either belongs to the controller (disks, network,
# description) or would let a configuration file run code on the host (args, hookscript).
PROFILE_QM_OPTIONS = frozenset({"cpuunits", "cpulimit", "cpu", "numa", "affinity"})
DISK_LIMITS = frozenset({"mbps_rd", "mbps_wr", "iops_rd", "iops_wr"})
STORAGE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
# What a template build can install; the toolchains sit where the setup-* actions look first.
TEMPLATE_COMPONENTS = ("docker", "gh", "node", "dotnet", "go", "python")
TEMPLATE_VERSIONS = {"node": "22.23.5", "go": "1.27.4", "python": "3.12.16", "dotnet": "9.0"}
_VERSION_PATTERNS = {
    "node": r"[0-9]+\.[0-9]+\.[0-9]+",
    "go": r"[0-9]+\.[0-9]+(?:\.[0-9]+)?",
    "python": r"3\.[0-9]+\.[0-9]+",
    "dotnet": r"[0-9]+\.[0-9]+|LTS|STS",
}

# The private and shared ranges the firewall template denies. The runner bridge must lie inside one of
# them, so the same denial covers the hypervisor's bridge address and every neighbouring VM.
PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")

# github.com's git-over-SSH ranges (https://api.github.com/meta, "git"); the only SSH egress allowed.
GITHUB_SSH_RANGES = ("192.30.252.0/22", "185.199.108.0/22", "140.82.112.0/20", "143.55.64.0/20")

# ZFS names hold 255 characters; the longest name the cache derives is
# <dataset>/jobs/<key>-<vmid>-<stamp>@g<stamp>.
MAX_CACHE_DATASET = 255 - len("/jobs/") - MAX_KEY_LENGTH - len("-999999999-@g") - 2 * len("99999999999")


@dataclass(frozen=True)
class GitHubConfig:
    """How the controller authenticates and which repositories it serves."""

    owner: str
    app_client_id: str
    app_key_file: Path
    api_url: str
    repositories: tuple[str, ...]


@dataclass(frozen=True)
class CapacityConfig:
    """How much of the host the runners may take."""

    memory_mb: int
    host_reserve_mb: int


@dataclass(frozen=True)
class NetworkConfig:
    """The private bridge the runner VMs live on."""

    bridge: str
    gateway: ipaddress.IPv4Address
    prefix: int
    dns: tuple[ipaddress.IPv4Address, ...]
    vlan: int | None
    rate: int | None

    @property
    def network(self) -> ipaddress.IPv4Network:
        """The subnet the gateway and every slot share."""
        return ipaddress.IPv4Network(f"{self.gateway}/{self.prefix}", strict=False)

    @property
    def nic(self) -> str:
        """The `net0` value of every VM on the bridge; the firewall flag makes its rules take effect."""
        tag = f",tag={self.vlan}" if self.vlan else ""
        rate = f",rate={self.rate}" if self.rate else ""
        return f"virtio,bridge={self.bridge},firewall=1{tag}{rate}"


@dataclass(frozen=True)
class Slot:
    """A VMID and address the controller may use for one runner VM."""

    vmid: int
    address: ipaddress.IPv4Address


@dataclass(frozen=True)
class Profile:
    """One VM shape, selected by a job through its match label."""

    name: str
    match: str
    labels: tuple[str, ...]
    cores: int
    memory_mb: int
    warm: int
    maximum: int
    qm_options: tuple[tuple[str, str], ...]
    disk_limits: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CacheMount:
    """A directory of the cache volume bound to a path inside the VM."""

    path: PurePosixPath
    directory: str
    owner: str


@dataclass(frozen=True)
class CacheConfig:
    """Where job caches live and how they are kept bounded."""

    backend: str
    dataset: str
    volume_size: str
    keep_snapshots: int
    retention_days: int
    unused_days: int
    wipe_above_mb: int
    docker_keep_storage_gb: int
    pool_high_water_percent: int
    min_write_mb: int
    mounts: tuple[CacheMount, ...]


@dataclass(frozen=True)
class FirewallConfig:
    """Extra egress rules and denied destinations on top of the deny-by-default template."""

    ssh_destinations: tuple[str, ...]
    extra_rules: tuple[str, ...]
    deny_destinations: tuple[str, ...]


@dataclass(frozen=True)
class OverflowConfig:
    """Which repository variable steers jobs to a hosted runner while the pool is full."""

    profile: str
    variable: str
    self_value: str
    hosted_value: str
    repositories: tuple[str, ...]
    on_backlog: int
    on_wait_seconds: int
    off_seconds: int


@dataclass(frozen=True)
class TemplateConfig:
    """How runner templates are built: two VMIDs that take turns, the base image and what goes on it."""

    vmids: tuple[int, int]
    storage: str
    image_url: str
    image_sha256: str
    address: ipaddress.IPv4Address
    snippet_storage: str
    disk_gb: int
    cores: int
    memory_mb: int
    components: tuple[str, ...]
    versions: tuple[tuple[str, str], ...]
    scripts: tuple[PurePosixPath, ...]

    def version(self, component: str) -> str:
        """The version a component is installed in."""
        return dict(self.versions)[component]


@dataclass(frozen=True)
class DaemonConfig:
    """Timing, naming and local paths of the controller."""

    poll_seconds: int
    idle_timeout_seconds: int
    boot_timeout_seconds: int
    max_job_seconds: int
    name_prefix: str
    log_dir: Path
    log_retention_days: int
    state_dir: Path


@dataclass(frozen=True)
class Config:
    """The whole validated configuration."""

    github: GitHubConfig
    capacity: CapacityConfig
    network: NetworkConfig
    slots: tuple[Slot, ...]
    profiles: tuple[Profile, ...]
    cache: CacheConfig
    firewall: FirewallConfig
    overflow: OverflowConfig | None
    template: TemplateConfig
    daemon: DaemonConfig

    def profile(self, name: str) -> Profile:
        """The profile with this name."""
        return next(p for p in self.profiles if p.name == name)


def load(path: Path = DEFAULT_PATH) -> Config:
    """Reads and validates the configuration file."""
    try:
        raw = path.read_bytes()
    except OSError as err:
        raise ConfigError(f"{path}: cannot be read ({err.strerror})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise ConfigError(f"{path}: not valid TOML ({err})") from None
    return parse(data)


def parse(data: dict[str, object]) -> Config:
    """Builds the configuration from parsed TOML and checks every cross-field rule."""
    root = Section(data, "")
    network = _network(_required(root, "network"))
    profiles = tuple(_profile(s) for s in root.sections("profile", minimum=1))
    config = Config(
        github=_github(_required(root, "github")),
        capacity=_capacity(_required(root, "capacity")),
        network=network,
        slots=tuple(_slot(s) for s in root.sections("slot", minimum=1)),
        profiles=profiles,
        cache=_cache(root.section("cache")),
        firewall=_firewall(root.section("firewall")),
        overflow=_overflow(root.section("overflow"), profiles),
        template=_template(_required(root, "template")),
        daemon=_daemon(root.section("daemon")),
    )
    root.done()
    _check(config)
    return config


def check_secret_file(path: Path) -> None:
    """Refuses a key file that someone other than its owner could read or replace."""
    info = _stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{path}: must be a regular file")
    if info.st_mode & 0o077:
        raise ConfigError(f"{path}: mode {stat.S_IMODE(info.st_mode):04o} is too open, use 0600")
    if info.st_uid != os.geteuid():
        raise ConfigError(f"{path}: must be owned by the user running the controller")


def _stat(path: Path) -> os.stat_result:
    try:
        return path.stat()
    except OSError as err:
        raise ConfigError(f"{path}: cannot be read ({err.strerror})") from None


def _required(root: Section, key: str) -> Section:
    section = root.section(key)
    if section is None:
        raise root.error(key, "is required")
    return section


def _github(s: Section) -> GitHubConfig:
    api_url = s.text(
        "api_url", default="https://api.github.com", pattern=r"https://[A-Za-z0-9.-]+(?:/[\w./-]*)?"
    )
    result = GitHubConfig(
        owner=s.text("owner", pattern=OWNER),
        app_client_id=s.text("app_client_id", pattern=r"[A-Za-z0-9.]{1,64}"),
        app_key_file=Path(s.absolute_path("app_key_file", default="/etc/pve-ci-runners/app.pem")),
        api_url=api_url.rstrip("/"),
        repositories=s.texts("repositories", default=(), pattern=REPOSITORY),
    )
    s.done()
    return result


def _capacity(s: Section) -> CapacityConfig:
    result = CapacityConfig(
        memory_mb=s.number("memory_mb", minimum=1024),
        host_reserve_mb=s.number("host_reserve_mb", default=4096, minimum=0),
    )
    s.done()
    return result


def _network(s: Section) -> NetworkConfig:
    result = NetworkConfig(
        bridge=s.text("bridge", pattern=r"[A-Za-z0-9_.-]{1,15}"),
        gateway=s.ipv4("gateway"),
        prefix=s.number("prefix", minimum=8, maximum=30),
        dns=tuple(
            ipaddress.IPv4Address(d) for d in s.texts("dns", minimum=1, maximum=3, pattern=r"[0-9.]{7,15}")
        ),
        vlan=s.number("vlan", minimum=1, maximum=4094) if s.has("vlan") else None,
        # Proxmox takes the NIC rate limit in MB/s.
        rate=s.number("rate", minimum=1, maximum=100_000) if s.has("rate") else None,
    )
    s.done()
    if not any(result.network.subnet_of(ipaddress.IPv4Network(r)) for r in PRIVATE_RANGES):
        raise s.error("gateway", f"the bridge network must lie inside one of {', '.join(PRIVATE_RANGES)}")
    return result


def _slot(s: Section) -> Slot:
    result = Slot(vmid=s.number("vmid", minimum=100, maximum=999_999_999), address=s.ipv4("address"))
    s.done()
    return result


def _profile(s: Section) -> Profile:
    name = s.text("name", pattern=NAME)
    match = s.text("match", pattern=LABEL)
    labels = s.texts("labels", pattern=LABEL, minimum=1, maximum=20)
    maximum = s.number("max", minimum=1, maximum=1000)
    qm_options = s.scalars("qm_options", key_pattern="|".join(sorted(PROFILE_QM_OPTIONS)))
    disk_limits = s.scalars("disk_limits", key_pattern="|".join(sorted(DISK_LIMITS)))
    result = Profile(
        name=name,
        match=match,
        labels=labels,
        cores=s.number("cores", minimum=1, maximum=512),
        memory_mb=s.number("memory_mb", minimum=1024),
        warm=s.number("warm", default=1, minimum=0, maximum=maximum),
        maximum=maximum,
        qm_options=tuple(sorted((k, _option_value(s, k, v)) for k, v in qm_options.items())),
        disk_limits=tuple(sorted((k, _positive(s, k, v)) for k, v in disk_limits.items())),
    )
    s.done()
    if "self-hosted" not in labels or match not in labels:
        raise s.error("labels", "must contain 'self-hosted' and the match label")
    return result


def _option_value(s: Section, key: str, value: str | int) -> str:
    text = str(value)
    if not text or any(c in text for c in ",=\n\r\0 "):
        raise s.error(f"qm_options.{key}", "must be a single value without separators")
    return text


def _positive(s: Section, key: str, value: str | int) -> int:
    if not isinstance(value, int) or value <= 0:
        raise s.error(f"disk_limits.{key}", "must be a positive integer")
    return value


def _cache(s: Section | None) -> CacheConfig:
    s = s or Section({}, "cache")
    backend = s.text("backend", default="zfs", pattern="zfs|none")
    result = CacheConfig(
        backend=backend,
        dataset=s.text(
            "dataset", default="", pattern=ZFS_DATASET if backend == "zfs" else r"[A-Za-z0-9/_.:-]*"
        ),
        volume_size=s.text("volume_size", default="20G", pattern=SIZE),
        keep_snapshots=s.number("keep_snapshots", default=2, minimum=1, maximum=10),
        retention_days=s.number("retention_days", default=30, minimum=1, maximum=3650),
        unused_days=s.number("unused_days", default=30, minimum=1, maximum=3650),
        wipe_above_mb=s.number("wipe_above_mb", default=17000, minimum=100),
        docker_keep_storage_gb=s.number("docker_keep_storage_gb", default=8, minimum=0),
        pool_high_water_percent=s.number("pool_high_water_percent", default=80, minimum=50, maximum=95),
        min_write_mb=s.number("min_write_mb", default=1, minimum=0),
        mounts=_mounts(s),
    )
    s.done()
    return result


DEFAULT_MOUNTS = (
    CacheMount(PurePosixPath("/home/runner/.nuget"), "nuget", "runner"),
    CacheMount(PurePosixPath("/home/runner/.npm"), "npm", "runner"),
    CacheMount(PurePosixPath("/home/runner/.cache/uv"), "uv", "runner"),
    CacheMount(PurePosixPath("/var/lib/docker"), "docker", "root"),
    CacheMount(PurePosixPath("/var/lib/containerd"), "containerd", "root"),
)


def _mounts(s: Section) -> tuple[CacheMount, ...]:
    if not s.has("mount"):
        s.sections("mount")
        return DEFAULT_MOUNTS
    mounts: list[CacheMount] = []
    for m in s.sections("mount", minimum=1):
        mounts.append(
            CacheMount(
                path=m.absolute_path("path"),
                directory=m.text("dir", pattern=r"[a-z0-9][a-z0-9_-]{0,31}"),
                owner=m.text("owner", default="runner", pattern="runner|root"),
            )
        )
        m.done()
    return tuple(mounts)


def _firewall(s: Section | None) -> FirewallConfig:
    s = s or Section({}, "firewall")
    destinations = s.texts("ssh_destinations", default=GITHUB_SSH_RANGES, maximum=64)
    for cidr in destinations:
        try:
            ipaddress.IPv4Network(cidr)
        except ValueError:
            raise s.error("ssh_destinations", f"{cidr!r} is not an IPv4 network") from None
    rule = r"(IN|OUT) (ACCEPT|DROP|REJECT)( -[a-z-]+ [A-Za-z0-9.,:/_-]+)*( # [ -~]{0,80})?"
    result = FirewallConfig(
        ssh_destinations=destinations,
        extra_rules=s.texts("extra_rules", default=(), pattern=rule),
        deny_destinations=_networks(s, "deny_destinations"),
    )
    s.done()
    return result


def _networks(s: Section, key: str) -> tuple[str, ...]:
    """IPv4 and IPv6 networks in their normal form, each once."""
    networks: list[str] = []
    for cidr in s.texts(key, default=(), maximum=256):
        try:
            network = str(ipaddress.ip_network(cidr))
        except ValueError:
            raise s.error(key, f"{cidr!r} is not an IPv4 or IPv6 network") from None
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def _overflow(s: Section | None, profiles: tuple[Profile, ...]) -> OverflowConfig | None:
    if s is None:
        return None
    profile = s.text("profile", default=profiles[0].name, pattern=NAME)
    chosen = next((p for p in profiles if p.name == profile), None)
    if chosen is None:
        raise s.error("profile", f"names no profile ({profile!r})")
    result = OverflowConfig(
        profile=profile,
        variable=s.text("variable", pattern=VARIABLE),
        self_value=s.text(
            "self_value", default=json.dumps(["self-hosted", chosen.match], separators=(",", ":"))
        ),
        hosted_value=s.text("hosted_value"),
        repositories=s.texts("repositories", pattern=REPOSITORY, minimum=1),
        on_backlog=s.number("on_backlog", default=2, minimum=1),
        on_wait_seconds=s.number("on_wait_seconds", default=120, minimum=0),
        off_seconds=s.number("off_seconds", default=60, minimum=0),
    )
    s.done()
    for key, value in (("self_value", result.self_value), ("hosted_value", result.hosted_value)):
        _require_runs_on_json(s, key, value)
    return result


def _require_runs_on_json(s: Section, key: str, value: str) -> None:
    # Workflows read the variable through fromJSON(), so it must be a JSON string or list of strings.
    try:
        decoded: object = json.loads(value)
    except ValueError:
        raise s.error(key, "must be JSON, a label string or a list of labels") from None
    labels = decoded if isinstance(decoded, list) else [decoded]
    if not labels or not all(isinstance(label, str) and label for label in labels):
        raise s.error(key, "must be JSON, a label string or a list of labels")


def _template(s: Section) -> TemplateConfig:
    first, second = s.numbers("vmids", count=2, minimum=100, maximum=999_999_999)
    result = TemplateConfig(
        vmids=(first, second),
        storage=s.text("storage", pattern=STORAGE),
        image_url=s.text("image_url", pattern=r"https://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+"),
        image_sha256=s.text("image_sha256", pattern=r"[0-9a-f]{64}"),
        address=s.ipv4("address"),
        snippet_storage=s.text("snippet_storage", default="local", pattern=STORAGE),
        disk_gb=s.number("disk_gb", default=40, minimum=8, maximum=4096),
        cores=s.number("cores", default=4, minimum=1, maximum=128),
        memory_mb=s.number("memory_mb", default=4096, minimum=1024, maximum=262144),
        components=s.texts("components", default=TEMPLATE_COMPONENTS, pattern="|".join(TEMPLATE_COMPONENTS)),
        versions=_versions(s),
        scripts=tuple(
            PurePosixPath(p) for p in s.texts("scripts", default=(), pattern=r"(?:/[A-Za-z0-9_.-]+)+")
        ),
    )
    if any(".." in path.parts for path in result.scripts):
        raise s.error("scripts", "paths must not contain '..'")
    s.done()
    return result


def _versions(s: Section) -> tuple[tuple[str, str], ...]:
    given = s.scalars("versions", key_pattern="|".join(TEMPLATE_VERSIONS))
    result: dict[str, str] = dict(TEMPLATE_VERSIONS)
    for name, value in given.items():
        if not isinstance(value, str) or re.fullmatch(_VERSION_PATTERNS[name], value) is None:
            raise s.error("versions", f"{name}: {value!r} is not a version")
        result[name] = value
    return tuple(sorted(result.items()))


def _daemon(s: Section | None) -> DaemonConfig:
    s = s or Section({}, "daemon")
    result = DaemonConfig(
        poll_seconds=s.number("poll_seconds", default=15, minimum=5, maximum=300),
        idle_timeout_seconds=s.number("idle_timeout_seconds", default=600, minimum=60),
        boot_timeout_seconds=s.number("boot_timeout_seconds", default=300, minimum=60),
        # Above GitHub's default job timeout of six hours, so only a VM that outlived its job is cut off.
        max_job_seconds=s.number("max_job_seconds", default=23400, minimum=3600, maximum=604800),
        name_prefix=s.text("name_prefix", default="ci-runner-", pattern=r"[a-z][a-z0-9-]{0,40}"),
        log_dir=Path(s.absolute_path("log_dir", default="/var/log/pve-ci-runners")),
        log_retention_days=s.number("log_retention_days", default=14, minimum=1, maximum=3650),
        state_dir=Path(s.absolute_path("state_dir", default="/var/lib/pve-ci-runners")),
    )
    s.done()
    if result.max_job_seconds <= result.idle_timeout_seconds:
        raise s.error("max_job_seconds", "must exceed idle_timeout_seconds")
    return result


def _check(config: Config) -> None:
    _check_slots(config)
    _check_addresses(config)
    _check_profiles(config)
    if len(config.cache.dataset) > MAX_CACHE_DATASET:
        raise ConfigError(f"cache.dataset: must be at most {MAX_CACHE_DATASET} characters long")


def _check_slots(config: Config) -> None:
    vmids = [s.vmid for s in config.slots] + list(config.template.vmids)
    if len(set(vmids)) != len(vmids):
        raise ConfigError("slot/template: every slot and template needs its own VMID")
    addresses = [s.address for s in config.slots]
    if len(set(addresses)) != len(addresses):
        raise ConfigError("slot: every slot needs its own address")
    if config.template.address in addresses:
        raise ConfigError("template.address: is a slot's address")


def _check_addresses(config: Config) -> None:
    network = config.network.network
    named = [(f"slot {s.vmid}", s.address) for s in config.slots] + [("template", config.template.address)]
    for name, address in named:
        if address not in network or address in {network.network_address, network.broadcast_address}:
            raise ConfigError(f"{name}: {address} is not a host address of {network}")
        if address == config.network.gateway:
            raise ConfigError(f"{name}: {address} is the gateway")


def _check_profiles(config: Config) -> None:
    _check_match_labels(config)
    if sum(p.maximum for p in config.profiles) > len(config.slots):
        raise ConfigError("profile: the sum of 'max' exceeds the number of slots")
    if sum(p.maximum * p.memory_mb for p in config.profiles) > config.capacity.memory_mb:
        raise ConfigError("profile: 'max' times 'memory_mb' exceeds capacity.memory_mb")


def _check_match_labels(config: Config) -> None:
    names = [p.name for p in config.profiles]
    matches = [p.match for p in config.profiles]
    if len(set(names)) != len(names) or len(set(matches)) != len(matches):
        raise ConfigError("profile: names and match labels must be unique")
    for profile in config.profiles:
        others = {p.match for p in config.profiles if p is not profile}
        if others & set(profile.labels):
            raise ConfigError(f"profile {profile.name}: carries another profile's match label")

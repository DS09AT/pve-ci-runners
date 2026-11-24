# pve-ci-runners

Run every GitHub Actions job in a fresh virtual machine on your own Proxmox VE host, with a warm,
copy-on-write cache per workflow job.

- **One job, one VM.** Each job gets a linked clone of a runner template and a single-use (JIT) runner
  registration. When the job ends, the VM is destroyed. Nothing a job leaves behind reaches the next one.
- **A cache that does not leak.** Each workflow job has its own ZFS volume. A job works on a private clone of
  it; only a successful run on the default branch writes its clone back. Pull requests read the cache but
  can never change what the default branch builds with.
- **Isolated by default.** A VM may reach the internet, but neither the hypervisor nor any private network.
  The controller proves this from inside the VM before it hands the VM to a job.
- **A good neighbour.** Runner VMs never squeeze the host's other guests: a memory budget, a host reserve and
  low CPU priority keep production VMs first.
- **No inbound port, no extra service.** One stdlib-only Python daemon on the Proxmox node polls GitHub as a
  GitHub App. It talks to the VMs only through the QEMU guest agent; the VMs run no SSH server.
- **Overflow when busy.** Optionally, before all VMs are busy, the controller points a repository variable at
  a hosted runner label, so queued work keeps moving.

> Status: alpha. Proxmox VE 9 on amd64, private repositories, one controller per Proxmox node.

## How it works

```
GitHub ──(App installation token, polling with ETags)──▶ pve-ci-runners on the Proxmox node
                                                          │
                  qm clone / qm set / qm guest exec ◀─────┘
                                                          │
         ┌──────────────── runner VM (linked clone) ──────┴──────────────┐
         │  JIT runner ─ one job ─ /cache (ZFS clone, hot-plugged)       │
         └──────── private bridge + NAT, Proxmox firewall per VM ────────┘
```

1. A slot VM is cloned from the current template, gets its firewall rules and boots.
2. Through the guest agent the controller sets its address, installs the runner unit, and proves that the VM
   cannot reach the hypervisor. Only then is the VM *warm*.
3. When a job asks for the VM's profile label, the controller registers a single-use runner for that
   repository and starts it in a warm VM.
4. Once the job has started, the controller asks GitHub which run this runner serves, clones the matching
   cache and hot-plugs it. The job waits for it before its first step.
5. When the job ends, the VM is destroyed. A successful default-branch run's cache becomes the new cache.

All state lives on the host (VM descriptions, ZFS properties), so the controller can be restarted at any
time, even while jobs run.

## Requirements

- Proxmox VE 9 (Debian 13), amd64, with the datacenter firewall enabled.
- A private bridge with NAT for the runner VMs (for example `vmbr1` on `192.168.100.0/24`). Its network
  must lie inside `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` or `100.64.0.0/10`, the ranges every
  runner VM's firewall denies.
- A storage that supports linked clones for the template and the slot VMs (ZFS, LVM-thin or RBD).
- For the cache: a ZFS pool. Without ZFS the controller runs with `cache.backend = "none"`.
- A storage with the `snippets` content type (for example `local`), used for cloud-init during template
  builds, and outbound internet access from the bridge while a template is built.
- A GitHub App installed on the account or organization whose private repositories should use the runners.

### GitHub App permissions

| Permission | Access | Why |
|---|---|---|
| Administration (repository) | Read and write | create single-use runner registrations and remove them |
| Actions (repository) | Read | list queued jobs and read which runner serves which job |
| Contents (repository) | Read | read the default branch's head commit, which proves that a run built that branch before its cache is written back; Administration already allows deploy keys, so this adds no reach |
| Metadata (repository) | Read | mandatory for every App |
| Variables (repository) | Read and write | only for overflow; leave it out otherwise |

The App needs no webhook, no user permissions and no organization permissions.

## Install

Each release ships a `.deb` with a SHA-256 sum and a build provenance attestation.

```sh
sha256sum -c SHA256SUMS
gh attestation verify pve-ci-runners_*_all.deb --repo DS09AT/pve-ci-runners
apt install ./pve-ci-runners_*_all.deb
```

The package enables nothing. Then:

1. Write `/etc/pve-ci-runners/config.toml` (see below) and store the App key as
   `/etc/pve-ci-runners/app.pem` with mode 0600.
2. `pve-ci-runners doctor` until every check passes.
3. `pve-ci-runners template build` for the first template (about 15 minutes).
4. `systemctl enable --now pve-ci-runners.service pve-ci-runners-template.timer`

A template build downloads the configured cloud image once, checks its pinned SHA-256, boots it behind the
runner firewall and provisions it through the guest agent: the newest Actions runner (checked against the
digest GitHub publishes for the release asset), Docker, the GitHub CLI and the Node.js, .NET, Go and Python
toolchains in the tool cache layout the `setup-*` actions look in first. Your own scripts from
`template.scripts` run last. The build writes the template VMID that is not in use and marks it finished
only at the end, so an interrupted build is never cloned. The build VM must fit the memory budget and
the host reserve like a runner VM; when it does not, the build drains the runners (queued jobs wait, or go
to the hosted runner when `[overflow]` is configured), waits up to two hours for room, and gives up
without starting anything otherwise. The older template stays until the next build, because running VMs
may still be linked clones of it.

## Configuration

`/etc/pve-ci-runners/config.toml`, owned by root. Unknown keys are errors. The package installs this example
as `/usr/share/doc/pve-ci-runners/config.example.toml`:

```toml
# /etc/pve-ci-runners/config.toml, owned by root with mode 0600. Unknown keys are errors.
# `pve-ci-runners config check` validates this file; `pve-ci-runners doctor` checks the host against it.

[github]
owner = "example-org"                         # the account the GitHub App is installed on
app_client_id = "Iv23liEXAMPLE"
app_key_file = "/etc/pve-ci-runners/app.pem"  # the App's private key, mode 0600
# repositories = ["example-org/app"]          # serve only these; default: every private repository

[capacity]
memory_mb = 20480        # all runner VMs together
host_reserve_mb = 4096   # never start a VM if the host would keep less than this for its other guests
# disk_reserve_gb = 10   # free disk space kept per runner or build VM before another one starts

[network]
bridge = "vmbr1"         # a NAT bridge that only runner VMs use
gateway = "192.168.100.1"
prefix = 24
dns = ["192.0.2.53"]
# rate = 50              # NIC limit of every runner and build VM in MB/s; default: none

[[slot]]
vmid = 121
address = "192.168.100.21"

[[slot]]
vmid = 122
address = "192.168.100.22"

[[profile]]
name = "default"
match = "pve-ci"                       # jobs select this profile with runs-on: [self-hosted, pve-ci]
labels = ["self-hosted", "linux", "x64", "pve-ci"]
cores = 4
memory_mb = 5120
warm = 1                               # VMs kept booted and isolated, ready for the next job
max = 2
qm_options = { cpuunits = 100 }        # lower CPU priority than the host's other guests
# disk_limits = { mbps_wr = 200, iops_wr = 2000 }   # IO limits of the root and the cache disk

[cache]
backend = "zfs"                        # or "none"
dataset = "rpool/ci-cache"
# free_reserve_gb = 30                 # evict caches below this; default: (slots + 1) * disk_reserve_gb

[template]
vmids = [9201, 9202]                   # two template VMIDs that take turns
storage = "local-zfs"                  # where templates and their linked clones live; sparse on ZFS
# disk_gb = 40                         # the template disk's size; thin, so only what is written counts
address = "192.168.100.20"             # the build VM's address on the bridge
# A dated cloud image and the SHA-256 published next to it; the build refuses any other bytes.
image_url = "https://cloud-images.ubuntu.com/releases/noble/release-20260101/ubuntu-24.04-server-cloudimg-amd64.img"
image_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
# components = ["docker", "gh", "node", "dotnet", "go", "python"]
# versions = { node = "22.23.5", go = "1.27.4", python = "3.12.16", dotnet = "9.0" }
# scripts = ["/etc/pve-ci-runners/provision.d/extra.sh"]   # run as root in the template, root-owned

# [firewall]                           # private, link-local, multicast and IPv6 egress is always denied
# deny_destinations = ["198.51.100.0/24", "2001:db8::/32"]  # denied too, like this host's own networks
# extra_rules = ["OUT DROP -dest 203.0.113.0/24 # example"]  # after the denials, so they cannot reopen one

# [overflow]                           # send jobs to a hosted runner while every slot is busy
# variable = "CI_RUNS_ON"
# self_value = '["self-hosted", "pve-ci"]'
# hosted_value = '"ubuntu-latest"'
# repositories = ["example-org/app"]
# on_backlog = 2                       # switch on once this many jobs have no VM,
# on_wait_seconds = 120                # once a job has waited this long,
# on_free_slots = 1                    # or once the profile could start no more than this many jobs
# off_free_slots = 3                   # switch off only while nothing waits and this many are free
# off_seconds = 300                    # without a break for this long
```

`pve-ci-runners config check` validates the file and exits; it is safe to use as a configuration
management `validate` step.

`capacity.host_reserve_mb` is kept free on top of the memory configured for every other guest on the node,
running or not, because a stopped VM may boot at any moment and runner VMs give way to it.
`pve-ci-runners doctor` fails its `guest memory` check when the physical memory cannot hold those guests,
the reserve and `capacity.memory_mb` together.

Runner VMs, template builds and job caches all write into thin volumes, so nothing but free space stops a
job from filling the dataset they share. The controller measures that space as ZFS reports it in
`available` for the datasets of `template.storage` and `cache.dataset`, which already honours each
dataset's quota and refquota, the quotas of its ancestors and the free space of the pool:

- A VM is created, or a warm VM takes a job, only while the space covers `capacity.disk_reserve_gb` for
  every VM that would then run. A template build waits for one reserve more than the runner VMs hold,
  and the controller keeps that reserve free while the build runs. The reserve, not `template.disk_gb`,
  therefore bounds what jobs may write before new work stops; a larger thin template disk costs nothing
  until it is written.
- As soon as the space holds fewer reserves than there are free slots, the least recently used caches
  that no running job uses are evicted in the same cycle until `cache.free_reserve_gb` is free again; only
  when even the present VMs lack their reserve are idle VMs retired as well. Jobs that still cannot get a
  VM count as unserved at once, so `[overflow]` takes them, and the controller measures again every cycle
  and resumes on its own once space is back. Space that cannot be measured starts and assigns nothing.
- The hourly maintenance evicts the same way, and also while the pool is above
  `cache.pool_high_water_percent`. A job only gets a cache while one more volume fits after eviction.

On a `zfspool` storage, set `sparse 1`; otherwise every disk Proxmox creates there, the template's
included, reserves its full size. `pve-ci-runners doctor` warns when the space cannot hold a reserve for
every slot and a template build, and fails when it cannot hold one.

A runner VM whose guest fails `daemon.unhealthy_checks` (default 3) checks in a row, for example because
its disk returns I/O errors or its guest agent no longer answers, is retired: its cache is discarded, its
runner registration is removed and its slot takes a new VM. These checks run in the background, so a guest
that hangs until its timeout holds up no other VM. A runner that has not started a job
`daemon.assign_timeout_seconds` (default 600) after it was registered is retired the same way, even while
jobs of its repository wait, so another VM serves them; without waiting jobs, an idle runner goes after
`daemon.idle_timeout_seconds` as before.

Every runner and build VM's firewall drops egress to the private, shared, link-local, multicast and IPv6
ranges, and to an IP set `deny` holding `firewall.deny_destinations` (IPv4 or IPv6 networks) plus every
address of the Proxmox node and the network each address sits on, as `ip -j addr` lists them when the
VM's firewall file is written; loopback and IPv6 link-local addresses are left out. These denials come
first, so neither `firewall.extra_rules` nor `firewall.ssh_destinations` can reopen a denied destination;
`extra_rules` can still drop more or allow public destinations ahead of the SSH and mail rules. Before a VM
takes a job, its isolation probe also tries one address in each of up to eight denied IPv4 networks.
The DNS servers in `network.dns` must lie outside every denied network, or the VMs cannot resolve names.

`network.rate` limits the NIC of every runner and build VM, in MB/s. A profile's `disk_limits`
(`mbps_rd`, `mbps_wr`, `iops_rd`, `iops_wr`) apply to the root disk and to the hot-plugged cache disk.

GitHub reads `vars.<variable>` once, when a workflow run is created, and every job of that run keeps the
value of that moment, even one queued long after `[overflow]` switched. Overflow therefore looks ahead:
besides `on_backlog` jobs without a VM and a job waiting `on_wait_seconds`, it switches on as soon as the
`[overflow]` profile could start no more than `on_free_slots` further jobs at once. That number counts
warm and booting VMs no queued job claims, plus the VMs that free slots, the profile's `max`, the memory
budget, the host reserve and the disk reserve still allow. It switches off only after nothing was unserved
and at least `off_free_slots` stayed free for `off_seconds` without a break, so the variable does not flap
while capacity hovers around one threshold. Both thresholds are at most the profile's `max`, and
`off_free_slots` must exceed `on_free_slots`; by default they are 1 and 3, lowered to fit a smaller `max`.
Draining, a failed isolation probe and jobs waiting without disk room switch overflow on regardless. Each
switch is logged with its reason, for example `overflow on: 1 slot free`.

## Commands

| Command | What it does |
|---|---|
| `pve-ci-runners run` | the controller, normally started by systemd |
| `pve-ci-runners config check` | validates the configuration |
| `pve-ci-runners doctor [--json]` | checks the host and the GitHub App; every failure names its fix |
| `pve-ci-runners template build` | builds the next runner template; the weekly timer runs it |
| `pve-ci-runners status [--json]` | what every slot does, read from the host |
| `pve-ci-runners drain` / `resume` | stop and resume taking new jobs; idle VMs are released, running jobs finish |
| `pve-ci-runners teardown [--purge-cache]` | removes every VM, template and runner registration of this node; see below |
| `pve-ci-runners version` | prints the version |

### Teardown

`pve-ci-runners teardown` drains the node, waits until no job runs (a job that outlived
`daemon.max_job_seconds` counts as done, as it does for the controller), then removes every VM at a
configured slot or template VMID that carries the `pve-ci-runners` tag and exactly the name the controller
gives a VM there, with its disks, firewall file and runner registration, plus leftover template build
snippets and the build's memory reservation. Any other guest, including one that took a configured VMID,
is left alone. Job caches are kept unless `--purge-cache` is given, which destroys the `jobs` and `repos`
datasets below `cache.dataset` but not `cache.dataset` itself. It fails while a template build runs,
exits non-zero if any step failed, and can be repeated until it succeeds. The node stays drained;
`resume` takes jobs again. To decommission a node, stop the timer and the service first, so no controller
cycle races the removal, then run `teardown`:

```sh
systemctl disable --now pve-ci-runners-template.timer pve-ci-runners.service
pve-ci-runners teardown --purge-cache
```

## Security model

- Jobs are untrusted code. They get root inside their VM and nothing outside it.
- Everything a VM reports is a hint; the controller decides from what the GitHub API reports.
- The App key stays on the host and is read by `openssl` only; tokens are short-lived.
- Public and internal repositories are never served: anyone, or every member of your enterprise, could open
  a pull request and run code on your host. Only repositories whose visibility is `private` are used.
- A runner VM is destroyed at the latest `daemon.max_job_seconds` (default 23400, 6.5 hours) after it took
  a job, whatever its guest reports.

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/DS09AT/pve-ci-runners/security/advisories/new), not in
public issues.

## Limitations

- One controller per Proxmox node; nodes do not share jobs yet.
- Runners are registered per repository; organization-level runner groups are not used.
- Polling every 15 seconds suits up to about 100 repositories per App installation.
- Self-hosted runner usage is free on GitHub today; GitHub has announced pricing changes before, so check
  the current terms for your plan.

## Development

```bash
uv sync
uv run pytest --cov --cov-report=json && uv run python scripts/check_quality.py
uvx pre-commit run --all-files
```

Changes go to `develop` through pull requests; `main` takes pull requests only. Commits follow
[Conventional Commits](https://www.conventionalcommits.org/), from which release-please writes the
[changelog](CHANGELOG.md) and prepares each release.

## License

[MIT](LICENSE)

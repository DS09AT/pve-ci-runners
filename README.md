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
- **Overflow when busy.** Optionally, while all VMs are busy, the controller points a repository variable at
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

## Configuration

`/etc/pve-ci-runners/config.toml`, owned by root. Unknown keys are errors.

```toml
[github]
owner = "example-org"
app_client_id = "Iv23liEXAMPLE"
app_key_file = "/etc/pve-ci-runners/app.pem"   # mode 0600

[capacity]
memory_mb = 20480        # all runner VMs together
host_reserve_mb = 4096   # never start a VM if the host would keep less than this for other guests

[network]
bridge = "vmbr1"
gateway = "192.168.100.1"
prefix = 24
dns = ["192.0.2.53"]

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
warm = 1
max = 2
qm_options = { cpuunits = 100 }        # lower CPU priority than the host's other guests

[cache]
backend = "zfs"
dataset = "rpool/ci-cache"

[template]
vmids = [9201, 9202]                   # two template VMIDs that take turns
storage = "local-zfs"
```

`pve-ci-runners config check` validates the file and exits; it is safe to use as a configuration
management `validate` step.

## Commands

| Command | What it does |
|---|---|
| `pve-ci-runners run` | the controller, normally started by systemd |
| `pve-ci-runners config check` | validates the configuration |
| `pve-ci-runners status [--json]` | what every slot does, read from the host |
| `pve-ci-runners drain` / `resume` | stop and resume taking new jobs; running jobs finish |
| `pve-ci-runners version` | prints the version |

## Security model

- Jobs are untrusted code. They get root inside their VM and nothing outside it.
- Everything a VM reports is a hint; the controller decides from what the GitHub API reports.
- The App key stays on the host and is read by `openssl` only; tokens are short-lived.
- Public and internal repositories are never served: anyone, or every member of your enterprise, could open
  a pull request and run code on your host. Only repositories whose visibility is `private` are used.
- A runner VM is destroyed at the latest `daemon.max_job_seconds` (default 23400, 6.5 hours) after it took
  a job, whatever its guest reports.

See [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

## Limitations

- One controller per Proxmox node; nodes do not share jobs yet.
- Runners are registered per repository; organization-level runner groups are not used.
- Polling every 15 seconds suits up to about 100 repositories per App installation.
- Self-hosted runner usage is free on GitHub today; GitHub has announced pricing changes before, so check
  the current terms for your plan.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

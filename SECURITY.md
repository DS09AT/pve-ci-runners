# Security policy

## Reporting a vulnerability

Please do not open a public issue. Report privately through GitHub's
[private vulnerability reporting](https://github.com/DS09AT/pve-ci-runners/security/advisories/new).
Include the version, your Proxmox VE version and the steps to reproduce. You get an answer within seven
days; fixes for confirmed issues are released as soon as they are ready and credited unless you prefer
otherwise.

Supported versions: the latest release.

## Threat model

`pve-ci-runners` runs untrusted code: every CI job may be hostile, including one from a compromised
dependency or a malicious pull request. The design assumes this and layers independent controls, so no
single mistake hands a job the hypervisor.

| Asset | Threat | Controls |
|---|---|---|
| The Proxmox host and its other guests | A job reaches the hypervisor or a neighbour over the network | Private bridge behind NAT; per-VM firewall dropping all inbound traffic and all private, link-local, multicast and IPv6 egress; address and MAC filter; an isolation probe from inside every VM to every address of the host before it takes a job; the controller refuses all new work if a probe succeeds |
| The host's memory and CPU | Runner or build VMs starve the host's other guests | Memory budget and host reserve, counting every running guest's promised memory, checked before every runner and build VM starts; a template build reserves its memory and drains the runners instead of squeezing other guests; every runner and build VM is the kernel's first out-of-memory victim, and is stopped again if that priority cannot be set; no ballooning, low CPU weight and optional IO limits per profile |
| Caches used by the default branch | A pull request poisons a cache a trusted build later executes | Each job writes a private ZFS clone; only a successful push, schedule or manual run of the default branch's current head commit in the repository itself may become the cache; the decision uses GitHub's record of the run, asked again when the job ends, never the VM's report or its stored state |
| Other repositories' caches | A job reads or writes another repository's cache | Cache keys are namespaced by repository, sanitized and end in a digest of the exact names; the cache is chosen from the run GitHub says the runner serves |
| The GitHub App key | Theft from the host or through logs | Key file mode 0600 and checked at start; read by `openssl` only; short-lived installation tokens; tokens never appear in logs or errors |
| Runner registrations | Reuse of a registration for a second job | Single-use JIT configurations, deleted inside the VM before the job starts; the VM is destroyed after one job, at the latest after `daemon.max_job_seconds` |
| The runner template | A tampered download ends up in every runner VM | The cloud image must match the SHA-256 pinned in the configuration; the Actions runner and the Python build must match the digest GitHub publishes for the release asset; Node.js and Go must match the checksums their projects publish; operator scripts must be root-owned and writable by no one else; the build VM sits behind the runner firewall, runs no SSH server, and gets the same isolation probe as a runner VM before the first provisioning step; before it, only cloud-init installs the guest agent and updates from the image's own signed package archive; no build starts while the datacenter firewall is off |
| The controller | Malformed input from configuration, API or guest | One strict parser per source; host commands are argument lists, never shell strings; guest inputs are shell-quoted after validation |

### Out of scope

- Escapes from a KVM guest to the host through a hypervisor vulnerability. Keep Proxmox VE updated.
- Public and internal repositories. They are never served, because anyone, or every member of the
  enterprise, can run code through a pull request.
- Secrets your own workflows hand to jobs. Treat every job as able to read them.

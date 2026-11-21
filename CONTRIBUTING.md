# Contributing

Thank you for helping. This file is the single source of the project's conventions; tools and
contributors follow it alike.

## Development setup

```bash
uv sync                      # Python 3.13 and the dev tools, no runtime dependencies
uv run pytest                # unit tests, coverage gate 90 %
uvx pre-commit install       # the hooks below run before every commit
uvx pre-commit run --all-files
```

Integration tests that need root and a ZFS kernel module are marked `zfs` and run in CI:
`sudo -E uv run pytest -m zfs`.

## Design rules

- **Stdlib only at runtime.** The controller runs on a hypervisor; every dependency is attack surface and an
  upgrade burden. Dev tools are fine.
- **Components behind narrow ports.** GitHub, Proxmox and the cache backend are reached through small
  interfaces (`typing.Protocol`). Decision logic (scheduling, overflow, cache promotion rules) is pure and
  takes plain data, so it is tested without a host.
- **One source per rule.** Labels, addresses, the firewall template and every guest script exist once. The
  template build and the runtime share the same guest scripts instead of copies.
- **Crash-only.** All state that must survive a restart lives on the host (VM descriptions, ZFS properties).
  A restarted controller rebuilds its view from there and converges.
- **Fail closed.** Unknown configuration keys, unverifiable guest reports and failed isolation probes stop
  the operation instead of being ignored.
- **Small modules, small functions.** Ruff's complexity limits in `pyproject.toml` are hard limits; split a
  module before widening it. No module names such as `utils`, `helpers` or `common`.

## Security rules

These follow defense in depth, zero trust, least privilege and need to know (OWASP ASVS, NIST SP 800-53
AC-6/SC-7/SI-10, NIST SP 800-190 for workload isolation). They are binding for every change.

- **Validate at the trust boundary.** Each input source has exactly one parser: the configuration file
  (`config`), GitHub API responses (`github`, `actions`) and anything read from a guest (`guest`). Values are
  checked there for type, range and allowed characters; the rest of the code trusts the parsed objects only.
- **Guests are hostile.** A job runs arbitrary code. Data reported from inside a VM is used only as a hint
  and verified against the GitHub API before it influences the host (for example which cache to attach).
- **No shell on the host.** Host commands are argument lists (`subprocess.run([...])`), never a shell string.
  Values interpolated into guest scripts are passed with `shlex.quote` after validation.
- **Secrets.** Read only from files owned by root with mode `0600`; never logged, never part of an exception
  message, never written into a VM except the single-use runner configuration, which the guest deletes before
  the job starts.
- **Least privilege.** The GitHub App gets only the permissions listed in the README. The firewall template
  denies every inbound connection and every private destination by default; exceptions are explicit
  configuration.
- **Isolation is proven, not assumed.** A VM is handed to a job only after a probe from inside it fails to
  reach the hypervisor.

## Code conventions

- Python 3.13, `ruff check`, `ruff format`, `mypy --strict`; no `# type: ignore` or `# noqa` without a one-line
  reason on the same line.
- Shell scripts: `bash`, `set -euo pipefail`, `shellcheck` clean, formatted with `shfmt -i 2 -ci`.
- Workflows: every action pinned to a full commit SHA with the version as a comment, minimal `permissions`
  per job, `actionlint` and `zizmor` clean.
- Names say what a thing is; functions say what they do. Avoid abbreviations except well-known ones
  (VM, VMID, CPU, JIT).

## Comments

- English, minimal, and only for the *why*: a workaround, an API constraint, a security reason, a
  non-obvious invariant.
- No comments that repeat the code, no commented-out code, no TODO without an issue link, no decorative
  separators.
- No references to internal documents, plans, history or tickets; state the reason inline so it stays
  true on its own.

```bash
# Wrong: repeats the code.
# Stop the VM
qm stop "$vmid"

# Right: explains a constraint the reader cannot see.
# A linked clone keeps its template busy; the template can only be removed after this.
qm destroy "$vmid" --purge 1
```

## Source hygiene

This repository is public. Never commit secrets, real host names, public IP addresses, account names,
e-mail addresses or local paths. Use placeholders: `example-org`, `pve1`, `vmbr1`, RFC 5737 addresses
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and RFC 1918 ranges.

## Tests

- Pure logic gets unit tests; the scheduler additionally gets invariant tests over seeded random states.
- The controller is tested as a state machine against in-memory fakes of its ports, including a restart in
  the middle of a job.
- A bug fix starts with a failing test.
- Coverage must stay at or above 90 % (branch coverage).

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/) in English: `feat:`, `fix:`, `docs:`, `ci:`,
  `test:`, `refactor:`, `build:`, `chore:`. Write a body that explains why.
- One logical change per pull request; CI must be green.
- Security issues are not reported in public issues; see [SECURITY.md](SECURITY.md).

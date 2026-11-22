# Changelog

All notable changes to this project are documented here. The entries up to 0.2.0 were written by hand when
automated release management ([release-please](https://github.com/googleapis/release-please)) was adopted;
every later entry is generated from [Conventional Commits](https://www.conventionalcommits.org/).

## [0.2.2](https://github.com/DS09AT/pve-ci-runners/compare/v0.2.1...v0.2.2) (2026-11-22)


### Bug Fixes

* keep the memory of every other guest free, running or not ([8a6b995](https://github.com/DS09AT/pve-ci-runners/commit/8a6b99525c2ec623bdd98b8de494bde5eb0a8274))
* keep the memory of every other guest free, running or not ([698530f](https://github.com/DS09AT/pve-ci-runners/commit/698530f74b0a06d6a39dd63e67b55659edcb11c3))

## [0.2.1](https://github.com/DS09AT/pve-ci-runners/compare/v0.2.0...v0.2.1) (2026-11-21)


### Continuous Integration

* release with release-please and keep a changelog ([4f9f55e](https://github.com/DS09AT/pve-ci-runners/commit/4f9f55e6f6cbb6d8319ee1fbbe34588038a44129))

## [0.2.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.1.1...v0.2.0) (2026-11-22)


### Features

* **doctor:** add a machine-readable report ([382308b](https://github.com/DS09AT/pve-ci-runners/commit/382308b3678ded60de0ba710a3d942a5d898f11a))


### Tests

* compare the version without importing a private module ([6d57256](https://github.com/DS09AT/pve-ci-runners/commit/6d57256a2d138dba034abc1d99bda0bf884fd142))

## [0.1.1](https://github.com/DS09AT/pve-ci-runners/compare/v0.1.0...v0.1.1) (2026-11-21)


### Bug Fixes

* count jobs as unserved while no template exists, so overflow can switch on ([26d234b](https://github.com/DS09AT/pve-ci-runners/commit/26d234b10ad42a116482294bd232e5231cef0984))
* reload systemd units when the package is installed or removed ([26d234b](https://github.com/DS09AT/pve-ci-runners/commit/26d234b10ad42a116482294bd232e5231cef0984))


### Code Refactoring

* keep every function at complexity five or below ([26d234b](https://github.com/DS09AT/pve-ci-runners/commit/26d234b10ad42a116482294bd232e5231cef0984))

## [0.1.0](https://github.com/DS09AT/pve-ci-runners/releases/tag/v0.1.0) (2026-11-21)


### Features

* add the controller core ([8242e44](https://github.com/DS09AT/pve-ci-runners/commit/8242e447e941e6574347101a2b28c49c294b4293))
* add template build, doctor and Debian packaging ([42b112c](https://github.com/DS09AT/pve-ci-runners/commit/42b112cabc1fb79c56c22c21d91c62c712ba76e1))


### Bug Fixes

* **cache:** keep write-back snapshot names above the origin ([7ee2276](https://github.com/DS09AT/pve-ci-runners/commit/7ee2276946fd03c29ad181b7cbc7e5e4503895c2))
* **packaging:** keep the exit status of every command ([db42819](https://github.com/DS09AT/pve-ci-runners/commit/db42819b4195ed5dbec18bf252d8d0103ade9d6d))
* never let runner or build VMs squeeze the host's other guests ([a944a59](https://github.com/DS09AT/pve-ci-runners/commit/a944a59aebfd8ea78e2c20db5e304055f3795894))
* **provision:** keep the Python version input ([a81bc7f](https://github.com/DS09AT/pve-ci-runners/commit/a81bc7fa729e8d674591d6c929fb8ff5f3b531e5))

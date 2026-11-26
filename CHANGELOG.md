# Changelog

All notable changes to this project are documented here. The entries up to 0.2.0 were written by hand when
automated release management ([release-please](https://github.com/googleapis/release-please)) was adopted;
every later entry is generated from [Conventional Commits](https://www.conventionalcommits.org/).

## [0.10.7](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.6...v0.10.7) (2026-11-26)


### Bug Fixes

* see runs that the status-filtered listing omits ([f0980d5](https://github.com/DS09AT/pve-ci-runners/commit/f0980d579ed709b307e607602d396c22f72b505c))

## [0.10.6](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.5...v0.10.6) (2026-11-26)


### Bug Fixes

* reroute re-run attempts too ([45b5b83](https://github.com/DS09AT/pve-ci-runners/commit/45b5b831440134af4a8433dd838b5b0d7ddefa95))

## [0.10.5](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.4...v0.10.5) (2026-11-26)


### Bug Fixes

* run the per-slot steps of a cycle in parallel ([4e6ed5e](https://github.com/DS09AT/pve-ci-runners/commit/4e6ed5ee38bbe0f54e83c1f18dcce273b23d317d))

## [0.10.4](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.3...v0.10.4) (2026-11-26)


### Bug Fixes

* read the repositories in parallel each cycle ([b5d68f4](https://github.com/DS09AT/pve-ci-runners/commit/b5d68f4561e34737e6931545227c22325ca85426))

## [0.10.3](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.2...v0.10.3) (2026-11-26)


### Bug Fixes

* reuse a slot sooner after its job ends ([7ace8cf](https://github.com/DS09AT/pve-ci-runners/commit/7ace8cf9c63defa9f6a9c1848a4df08b340b2f52))

## [0.10.2](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.1...v0.10.2) (2026-11-26)


### Bug Fixes

* publish only slots that can take a job now ([1c15be3](https://github.com/DS09AT/pve-ci-runners/commit/1c15be39813f26126c07e45287e0ec499c835e98))

## [0.10.1](https://github.com/DS09AT/pve-ci-runners/compare/v0.10.0...v0.10.1) (2026-11-26)


### Bug Fixes

* count recycled slots as free capacity ([7adcf83](https://github.com/DS09AT/pve-ci-runners/commit/7adcf8366aff3676e385d6bfd6326bb6faf79aee))

## [0.10.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.9.0...v0.10.0) (2026-11-26)


### Features

* publish the free capacity for per-job routing ([86cfd73](https://github.com/DS09AT/pve-ci-runners/commit/86cfd738c862deaa144979db198c433393e83808))

## [0.9.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.8.0...v0.9.0) (2026-11-26)


### Features

* let overflow switch on only after a job waited ([e149e65](https://github.com/DS09AT/pve-ci-runners/commit/e149e654d089c7131c225aa201c5c19f69f3d537))

## [0.8.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.7.0...v0.8.0) (2026-11-26)


### Features

* reroute pull request runs that already started ([d480e05](https://github.com/DS09AT/pve-ci-runners/commit/d480e0530690b2d61a136ecc968cd3d242265c98))

## [0.7.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.6.0...v0.7.0) (2026-11-24)


### Features

* reroute runs bound to full self-hosted runners ([fb1999a](https://github.com/DS09AT/pve-ci-runners/commit/fb1999ad286c6a7add2783eeff2e75fb589edadb))

## [0.6.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.5.3...v0.6.0) (2026-11-24)


### Features

* switch overflow on before the runners are full ([6c3cf60](https://github.com/DS09AT/pve-ci-runners/commit/6c3cf60067f03c62a1ef1c16392314389f3632c6))

## [0.5.3](https://github.com/DS09AT/pve-ci-runners/compare/v0.5.2...v0.5.3) (2026-11-23)


### Bug Fixes

* find queued jobs in pending, waiting and requested runs ([57fbafa](https://github.com/DS09AT/pve-ci-runners/commit/57fbafaf139b8ee83cd0affa79c6b116f06e7553))

## [0.5.2](https://github.com/DS09AT/pve-ci-runners/compare/v0.5.1...v0.5.2) (2026-11-23)


### Bug Fixes

* fail doctor while the guest bridge follows its ports ([4640dc7](https://github.com/DS09AT/pve-ci-runners/commit/4640dc71e335e67dd5fbb6c9970b6a421944d322))

## [0.5.1](https://github.com/DS09AT/pve-ci-runners/compare/v0.5.0...v0.5.1) (2026-11-23)


### Tests

* keep the ZFS integration caches clear of the free reserve ([28dcdc0](https://github.com/DS09AT/pve-ci-runners/commit/28dcdc0012665b2cfbdc3a4eb5b13edad91df0d7))

## [0.5.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.4.0...v0.5.0) (2026-11-23)


### Features

* keep runners within their disk space and heal broken VMs ([e916af6](https://github.com/DS09AT/pve-ci-runners/commit/e916af6099ded0dc989a80faa6dc11aa7fde234f))

## [0.4.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.3.3...v0.4.0) (2026-11-22)


### Features

* keep the uv cache between jobs ([5849151](https://github.com/DS09AT/pve-ci-runners/commit/5849151e0e290de5f0788b36b3afbd2b1cca5cee))

## [0.3.3](https://github.com/DS09AT/pve-ci-runners/compare/v0.3.2...v0.3.3) (2026-11-22)


### Bug Fixes

* tidy finished VMs in the background ([e8ad64d](https://github.com/DS09AT/pve-ci-runners/commit/e8ad64dcc4e0efbe5a83f87102cfcd88fe4287b6))

## [0.3.2](https://github.com/DS09AT/pve-ci-runners/compare/v0.3.1...v0.3.2) (2026-11-22)


### Bug Fixes

* re-assert the overflow variable and tolerate a VM gone at destroy ([da935f8](https://github.com/DS09AT/pve-ci-runners/commit/da935f8986c10ef1e50911426dc569141282d315))

## [0.3.1](https://github.com/DS09AT/pve-ci-runners/compare/v0.3.0...v0.3.1) (2026-11-22)


### Bug Fixes

* read the datacenter firewall option from cluster.fw ([8e94381](https://github.com/DS09AT/pve-ci-runners/commit/8e943812c4c8873be0335655eb8d9bded1c161c1))

## [0.3.0](https://github.com/DS09AT/pve-ci-runners/compare/v0.2.2...v0.3.0) (2026-11-22)


### Features

* deny the host's own networks to runners and add teardown ([ca9cf67](https://github.com/DS09AT/pve-ci-runners/commit/ca9cf672af8e2bce94134dc297fd03f30adad6de))

## [0.2.2](https://github.com/DS09AT/pve-ci-runners/compare/v0.2.1...v0.2.2) (2026-11-22)


### Bug Fixes

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

# Contributing to BSWorld

[English](CONTRIBUTING.md) | [中文](docs/zh-CH/CONTRIBUTING.md)

Thank you for contributing to BSWorld. Contributions may include bug fixes, tests, documentation, new Bluetooth LE Application environments, platform integrations, and improvements to the deterministic runtime or RF orchestration.

BSWorld's first-party code is licensed under Apache License 2.0. Imported or reused components retain their original licenses and are identified in their source files and [Third-Party Notices](THIRD_PARTY_NOTICES.md). All commits use the [Developer Certificate of Origin](https://developercertificate.org/) process and record the contributor's certification with a `Signed-off-by` line.

## Before You Start

- Search existing issues before opening a new one.
- Use [SECURITY.md](SECURITY.md) for vulnerabilities. Do not disclose security-sensitive details in a public issue.
- Open an issue before starting a substantial architectural change, new backend, or broad API change so the design and scope can be discussed first.
- Small fixes, focused tests, and documentation corrections may be submitted directly as a pull request.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md) in all project spaces.

## Development Setup

We recommend the [Docker test image](docker/README.md#run-tests) with your checkout mounted at `/workspace` for lint, unit tests, and integration tests. To change firmware or build configuration, [build your own image](docker/README.md#build).

## Making Changes

- Keep each change focused on one behavior or problem. Avoid unrelated refactoring and generated-file churn.
- Follow the existing separation between reusable framework code in `src/bsworld/` and Application-specific profiles, build files, and scenarios under `tests/`.
- Use standard asyncio APIs for asynchronous test work. Do not use blocking waits such as `time.sleep()` in an async test.
- Add or update tests for externally visible behavior. Prefer assertions on public outcomes over private implementation details.
- Update English and Chinese documentation together when behavior, commands, or public interfaces change.
- Do not commit locally compiled binaries, caches, or test logs.
- Add the Apache-2.0 copyright header used by neighboring files to new first-party source, configuration, and script files. Preserve upstream license headers in third-party code and update `THIRD_PARTY_NOTICES*` when introducing a new external component.

## Validation

Run the checks relevant to the change in a [Docker shell](docker/README.md#shell) with your checkout mounted, or in the local Python environment. At minimum, framework changes should pass:

```sh
ruff check --no-cache src tests
pytest tests/unit/
```

Run the affected virtual-system suite when changing process startup, scheduling, HCI, RF behavior, platform configuration, or test Applications. For example:

```sh
pytest tests/integration/nuttx/btsak -n auto
pytest tests/integration/nuttx/nimble -n auto
pytest tests/integration/zephyr/samples -n auto
pytest tests/integration/bumble -n auto
```

Use the full suite when the change crosses subsystem boundaries:

```sh
pytest tests/ -n auto
```

If a required suite cannot be run locally, state that explicitly in the pull request and explain why.

## Pull Requests

A pull request should:

- explain the problem and why the change is needed;
- describe the resulting behavior, including compatibility implications;
- list the exact validation commands and results;
- include matching tests and documentation where applicable;
- identify new dependencies, copied code, generated assets, or license implications;
- avoid credentials, private logs, machine-specific paths, and unrelated binary files.

Review may request that a large change be split into smaller commits. Commit messages should be concise, use the imperative mood, and describe the externally meaningful change rather than the editing process.

## Developer Certificate of Origin

BSWorld uses the Developer Certificate of Origin 1.1 instead of a separate contributor license agreement. Every commit in a pull request must contain a sign-off line with the contributor's real name and email address:

```text
Signed-off-by: Jane Doe <jane@example.com>
```

Add it automatically when creating a commit:

```sh
git commit --signoff
```

To sign an existing local commit, amend it before publishing the pull request:

```sh
git commit --amend --signoff --no-edit
```

The sign-off certifies your agreement to the following declaration:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
1 Letterman Drive
Suite D4700
San Francisco, CA, 94129

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

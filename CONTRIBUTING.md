# Contributing to BSWorld

[English](CONTRIBUTING.md) | [简体中文](docs/zh-CH/CONTRIBUTING.md)

BSWorld welcomes bug fixes, tests, documentation, Bluetooth LE application environments, platform integrations, and improvements to the deterministic runtime or RF orchestration.

BSWorld's own code uses Apache License 2.0. Third-party components retain the licenses recorded in their sources and in the [Third-Party Notices](THIRD_PARTY_NOTICES.md).

## Before You Start

- Search existing issues before opening a new one.
- Report vulnerabilities privately using the [security reporting process](SECURITY.md).
- Open an issue to discuss the design and scope before starting a substantial architectural change, new backend, or broad API change.
- Small fixes, focused tests, and documentation corrections may be submitted directly as a pull request.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md) in all project spaces.

## Development Setup

Set up the [local Linux environment](README.md#local-linux) for development.

To test a checkout in Docker, mount it when running a [published image](docker/README.md#run-tests). For firmware or Dockerfile changes, [build the image from your checkout](docker/README.md#build).

## Making Changes

- Keep each change focused on one behavior or problem. Leave unrelated refactoring and generated file changes out of the pull request.
- Keep reusable framework code in `src/bsworld/`. Put application-specific profiles, build files, and scenarios under `tests/`.
- Use standard asyncio APIs, such as `asyncio.sleep()` for waits, in async tests.
- Add or update tests for externally visible behavior and assert the public results where possible.
- Update English and Chinese documentation together when behavior, commands, or public interfaces change.
- Keep locally compiled binaries, caches, and test logs out of commits.
- Use the Apache-2.0 copyright header from neighboring files for new first-party source, configuration, and scripts. Preserve upstream license headers and update `THIRD_PARTY_NOTICES*` when introducing an external component.

## Validation

Run the checks relevant to your change locally or in Docker. See [Verifying Changes](docs/development.md#verifying-changes) for how to select an image.

Framework changes need to pass at least the lint, formatting, and unit checks.

```sh
ruff check --no-cache .
ruff format --check --no-cache .
pytest tests/unit
```

Follow the repository's [Python code style](docs/development.md#code-style). CI runs `Checks` for pull requests to `main` and pushes to `main`, covering lint, formatting, and unit tests.

Run the affected integration tests when changing process startup, scheduling, HCI, RF behavior, platform configuration, or test applications. With the integration environment prepared, run the suite from the repository root.

```sh
pytest tests/integration -n auto
```

For changes that cross subsystem boundaries, run all tests under `tests/`.

```sh
pytest tests/ -n auto
```

If a required suite cannot be run locally, state that explicitly in the pull request and explain why.

## Pull Requests

- Explain the problem and why the change is needed.
- Describe the resulting behavior and any compatibility implications.
- List the exact validation commands and results.
- Include matching tests and documentation where applicable.
- Identify new dependencies, copied code, generated assets, and license implications.
- Keep credentials, private logs, machine-specific paths, and unrelated binary files out of the pull request.

Reviewers may ask you to split large changes into smaller commits. Write concise, imperative commit messages that describe the change and its effect.

## Developer Certificate of Origin

BSWorld uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/) for contributor certification. Each pull request commit must carry a `Signed-off-by` line with the contributor's real name and email address.

```text
Signed-off-by: Jane Doe <jane@example.com>
```

Use `--signoff` to add the line when creating a commit.

```sh
git commit --signoff
```

To sign an existing local commit, amend it before publishing the pull request.

```sh
git commit --amend --signoff --no-edit
```

The sign-off certifies your agreement to the declaration below.

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

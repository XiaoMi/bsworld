# Security Policy

[English](SECURITY.md) | [简体中文](docs/zh-CH/SECURITY.md)

## Supported Versions

Security fixes are developed on the default branch and included in the next release. The latest published release is evaluated for relevant fixes; older releases do not receive routine security maintenance.

| Version | Support status |
|---|---|
| Default branch | Supported |
| Latest release | Supported |
| Earlier releases | No routine maintenance |

## Reporting a Vulnerability

Do not report a suspected vulnerability in a public issue, pull request, discussion, or log attachment.

Use the repository hosting service's private vulnerability-reporting feature when it is available. Otherwise, email [menglingao@xiaomi.com](mailto:menglingao@xiaomi.com) with the subject `[BSWorld Security]`. Include:

- the affected release, commit, and component;
- the security impact and expected attack conditions;
- reproducible steps or a minimal proof of concept;
- relevant configuration and sanitized logs;
- any suggested mitigation, if known.

Remove credentials, personal information, and unrelated proprietary data before sending a report. If the report itself requires sensitive attachments, first request a suitable private transfer method.

The maintainers will acknowledge the report, investigate its scope, coordinate a fix, and agree on disclosure timing with the reporter. No fixed response or resolution time is guaranteed, but reports will be handled as promptly as project resources allow. Credit will be given in the advisory or release notes when requested and appropriate.

## Scope

This policy covers security issues in BSWorld's first-party source, test orchestration, Docker build environment, and published project artifacts. Vulnerabilities that exist independently in BabbleSim, Zephyr, NuttX, Linux, or another upstream dependency should normally be reported to that project. Report the issue to BSWorld when it is caused by this repository's integration, packaging, or configuration, or when the correct ownership is unclear.

Ordinary functional bugs without a security impact should be reported through the public issue tracker.

## Coordinated Disclosure

Please allow time to reproduce the issue, prepare a fix, and update affected release artifacts before public disclosure. The maintainers will share status with the reporter and publish an advisory when users need to take action.

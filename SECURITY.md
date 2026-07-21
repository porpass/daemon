# Security Policy

We take the security of our project seriously. If you believe you have found a security vulnerability, please do not report it publicly via GitHub issues. Instead, follow the process outlined below.

## Supported Versions

The table below details the project versions that actively receive security updates and patches.


| Version | Supported          |
|---------| ------------------ |
| 0.1.x (alpha) | :white_check_mark: Yes |

## Reporting a Vulnerability

To report a vulnerability responsibly, please follow these steps:

1. **Submit Privately**: Navigate to the **Security** tab of this repository on GitHub, click **Report a vulnerability**, and fill out the form. Alternatively, email your findings to `porpass-admin@psi.edu`.
2. **Include Details**: Provide a clear description of the issue, the exact location of the problematic code, a working proof-of-concept (PoC), and potential impacts.
3. **Wait for Disclosure**: Do not publish information about the vulnerability until we have verified and patched it.

## Our Response Process

The daemon is maintained by a small team during the alpha, so please allow a
few working days for an initial reply. Once a report is received we will:

* **Acknowledge Receipt**: Confirm we received your report.
* **Assess & Verify**: Evaluate the severity and scope of the issue.
* **Develop a Patch**: Build and test a fix privately, using a private GitHub
  Security Advisory where appropriate.
* **Release & Disclose**: Publish a patched version along with a public Security Advisory crediting you for the discovery (if desired).

## Scope & Non-Vulnerabilities

This policy covers the daemon's own code, packaging, and deployment
documentation. Reports are most relevant where they concern how the daemon
handles credentials, stages files fetched from remote archives, or invokes
GRaSP as a subprocess.

The following types of issues are considered outside the scope of our security policy:
* Outdated third-party dependencies without a practical exploit path in our code.
* Issues that require pre-existing privileged access to the host, the database,
  or the shared storage mount.
* Operator configuration choices — for example an environment file left
  world-readable, or over-broad database privileges granted to the daemon's
  account. The deployment documentation covers the intended hardening.
* Vulnerabilities in GRaSP itself, which should be reported to that project.
* Vulnerabilities introduced entirely by custom modifications or third-party plug-ins.

Thank you for helping keep this project safe for everyone!

---

<sub>Portions of this documentation were drafted with assistance from Claude Opus 4.8 (Anthropic), July 2026.</sub>

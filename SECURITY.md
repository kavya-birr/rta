# Security Policy

## Supported Versions

openreversefeed is in alpha. Only the latest `main` branch is supported.
Once we tag a stable release, we will list supported minor versions here.

## Reporting a Vulnerability

**Please do not open a public issue for security problems.**

Report via GitHub's private advisory flow:
https://github.com/AngelOneWealth/openreversefeed/security/advisories/new

Or, if you need to reach us by email, use the security contact listed on the
organisation profile. Include:

- The affected file / path / function
- The version (commit SHA)
- A reproduction scenario or proof-of-concept
- Any known downstream impact

We aim to acknowledge reports within **2 business days** and provide a
remediation timeline within **7 business days**.

## Scope

In scope:

- Bugs in the library (`src/openreversefeed/`) that allow data corruption,
  privilege escalation, SQL injection, unauthorised access, or bypass of the
  composite-key dedup primitive.
- Vulnerabilities in the shipped adapters that cause incorrect financial
  calculations on well-formed input.
- Dependency CVEs that affect the library's default install.

Out of scope:

- Issues in the Django reference app under `examples/` that are configured
  for local development only (no auth, `DEBUG=True`, SQLite session store).
  These are demo scaffolding and are not intended for production use.
- Findings that depend on an operator running the library against a
  Postgres instance they do not control.

## Public Safety Guarantees

- No real investor PAN numbers, real ISIN codes, real AMC names, or
  registrar-supplied sample files are committed to this repository.
- The `tools/check_forbidden_strings.py` CI guardrail fails the build if any
  of those patterns appear in a tracked file.
- All external inputs are processed through validators before reaching the
  database layer.

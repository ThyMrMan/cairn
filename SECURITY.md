# Security Policy

## Reporting a vulnerability

Please report privately rather than in a public issue — use GitHub's **Report a
vulnerability** button under the Security tab, which opens a private advisory.

Include what you did, what happened, and what you expected. A proof of concept
helps; a working exploit is not required.

## Scope

Cairn is a single-user, self-hosted application. It is designed to sit on a LAN
or behind a tunnel, not on the open internet — see
[docs/11 — Security](docs/11-security.md) for the full threat model.

**In scope**

- Anything letting an archived page reach the app's origin, session cookie or
  API. Replay executes untrusted JavaScript by definition, and the separation
  between the two origins is the control that makes that safe.
- Authentication and session handling: bypass, fixation, privilege escalation
  past the single-account boundary, CSRF, rate-limiter evasion.
- Sealed-secret handling: cookie jars, TOTP secrets and recovery codes leaking
  into an API response, a log line, an export, or a WACZ.
- Path traversal or containment escapes out of the archive tree, including via
  symlinks or engine-supplied artifact paths.
- SSRF beyond what is documented as intentional. Fetching user-typed URLs is
  the product; fetching URLs extracted from archived HTML is checked against
  private ranges after DNS resolution, and a bypass of *that* is a finding.
- Anything in the container that runs as root, or escapes it.

**Out of scope**

- Consequences of mounting the Docker socket for container engines. It grants
  root-equivalent control of the host, this is stated wherever it is offered,
  and it is off by default.
- Running the app and replay on the same hostname. Ports do not isolate
  cookies; the app warns at startup when it detects this.
- Exposing the instance directly to the internet without a proxy or a tunnel,
  against the documented advice.
- A WARC containing the `Cookie:` header that fetched it. That is inherent to
  the format, is warned about before any capture using an account profile, and
  cannot be fixed without recording something other than what happened.
- Denial of service by pointing the crawler at something enormous. The limits
  are yours to set.

## Supported versions

The most recent release. This is a personal project with one maintainer; fixes
land on `main` and go out in the next image.

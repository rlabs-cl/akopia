# Security Policy

## Supported Versions

akopia is a rolling project. Only the `main` branch is supported
with security fixes. There are no LTS branches or backports.

| Version | Supported |
|---------|-----------|
| `main`  | Yes       |
| Older commits | No  |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security problems.**

Email **ramonlabbe@gmail.com** with subject line
`[akopia security]`. Please include:

- Affected version or commit SHA
- Environment (Docker Compose / k8s / bare process)
- Minimal reproduction steps
- Impact assessment (what can an attacker do?)
- Any proposed fix or mitigation, if you have one

If you would like to encrypt the report, mention it in the first
email and we will arrange a key exchange out-of-band.

## Response SLA

- **Acknowledgement:** within 72 hours of your report.
- **Triage and severity assessment:** within 7 days.
- **Fix timeline:** communicated after triage, scaled to severity.

We will keep you informed of progress and credit you in the release
notes unless you prefer to remain anonymous.

## Scope

In scope:

- The akopia source code in this repository
- Default Docker Compose deployment (`docker compose up`)
- The MCP server, concentrador, adapters, and extractors

Out of scope:

- Third-party services (Qdrant, Meilisearch, Redis, Ollama) — report
  to their upstream projects
- Self-hosted misconfigurations (e.g., exposing the bearer token)
- Denial of service via resource exhaustion on a single-node compose
  stack (by design — tune via your own infra controls)

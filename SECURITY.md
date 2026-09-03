# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately — do not open a public issue:

- GitHub: **Security → Report a vulnerability** on this repository (private
  vulnerability reporting), or
- email: **support@and-research.com**

We will acknowledge within a few business days. Please include reproduction
steps and the deployment configuration involved (mode, stage, fail posture).

## Deployment scope (read before reporting)

- **The proxy has no authentication of its own** and is designed as a
  sidecar / internal-network process; TLS terminates on customer
  infrastructure. Exposure to the public internet is unsupported (see the
  README "security" section) — reports assuming a public-internet deployment
  are out of scope.
- **API keys are transit-only**: the upstream key is never stored, logged, or
  included in audit events; the opt-in observation event carries only a
  SHA-256 hash of the client's bearer token.

## Not vulnerabilities

- Refusals of the documented semantic families and the stated claim limits —
  `docs/claims_scope.md` is the authority on what is and is not guaranteed.
- `flag` mode shipping an ungrounded answer unchanged (that is the mode's
  contract), and fail-`open` shipping an unverified answer when the voucher's
  own machinery fails (that is the operator's recorded governance choice).

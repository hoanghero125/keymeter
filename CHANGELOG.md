# Changelog

## 0.1.0 (unreleased)

First public release.

- `keymeter web`: browser dashboard with spend vs. budget, burn rate, run-out time, projected spend at
  reset, spend-over-time charts, spend per model, limits, an event log, and optional sound and desktop
  alerts. Listens on localhost only unless `--host` says otherwise.
- `keymeter tui`: the same numbers as a live terminal dashboard, plus the team's budget and keys on
  LiteLLM when the key may read them.
- `keymeter json`: one snapshot as JSON, exit code 1 when the key is not usable.
- Gateways: LiteLLM proxy (`/key/info`) and OpenRouter (`/api/v1/key`), picked automatically from the
  URL or the key.

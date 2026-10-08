# Changelog

## 0.1.1 (2026-10-08)

- A used-up budget reads as used up even when nothing is being spent: `runs_out_in_s` is 0, and the
  dashboards say "used up" instead of "measuring…" or "not at this rate". A key with a $0 budget reads
  `OVER BUDGET` even when the gateway reports no spend (it read `OK`).
- A budget that runs out in under half a second no longer reads as used up in the web dashboard and
  `json`: `runs_out_in_s` is at least 1 while some budget is left.
- When a budget reset is overdue, the run-out time no longer says the budget resets first.
- A NaN or infinite budget from the gateway counts as no budget (it broke the web dashboard, or read
  `OVER BUDGET` when negative).
- A NaN spend from the gateway, or a run-out time too far off to count (a huge budget meant as unlimited,
  with a slow burn), no longer stops the web dashboard from updating, and `json` writes a NaN spend as
  null instead of invalid JSON.
- New status `TEAM OVER BUDGET` when the team, not the key, is over its budget (it read as the key's
  `OVER BUDGET`). A blocked team's message reads "gateway reports the team as blocked".
- A team exactly at its budget reads `OK`, as in LiteLLM, which refuses a team only once its spend
  passes the budget (it read `OVER BUDGET`, and `json` exited with 1). A key at its budget is still over.
- A LiteLLM error that names the team, such as "Team=t1 is blocked", reads `TEAM BLOCKED` or
  `TEAM OVER BUDGET` with the gateway's own message (it read `BLOCKED` or `OVER BUDGET`). Scripts that
  check the `json` status should allow for the `TEAM` forms.
- Per-model budgets no longer compare the limit with all-time spend. LiteLLM reports the spend in the
  limit's period from 1.90 on; older versions show the limit without a percentage or alerts.
- Per-model limits that come from a key's budget tier are shown too.
- A `model_spend`, `model_max_budget` or `model_max_budget_usage` that is not a JSON object counts as
  empty instead of crashing keymeter.
- A team's `keys` from `/team/info` that is not a list counts as empty, and a key name or token that is a
  number shows as text, instead of crashing `json` and `tui`.
- A bare `https://openrouter.ai` URL works: keymeter adds `/api` for openrouter.ai and its subdomains.
  Other URLs, such as a proxy's, are used as given.
- An empty variable in the environment, such as `KEYMETER_KEY=` or cmd's `set KEYMETER_URL=""`, counts as
  unset, so it no longer hides the value in the `.env` file. `KEYMETER_URL` and `KEYMETER_GATEWAY` drop the
  quotes that cmd's `set NAME="value"` keeps, as `KEYMETER_KEY` did; a quoted URL or gateway was refused.
- `--help`, the README and `.env.example` say how `auto` picks the gateway: from the URL's host, or from
  the key only when no URL is set. `--help` and `.env.example` also say that `./.env` is read only when
  `KEYMETER_KEY` is not set in the environment.
- `keymeter web` answers a malformed request target or `Host` header with 400 instead of a traceback.
- README links work on the PyPI page.
- Web: switching theme, range, chart/table view or alerts crossfades; new data makes the big numbers
  count and the bars and charts glide instead of flashing. Reduced motion now stops only the looping
  spinner and pulse.
- Web: an error while drawing the page no longer shows as a lost server connection.
- Web: a key budget below zero no longer breaks the dashboard; the budget tile shows the spend and says
  the budget is below zero.
- Web: the tab title says "Offline" and the icon turns to a full red ring while the keymeter server
  can't be reached.
- Web: keyboard focus stays on the range buttons and "Poll now" after you use them.
- Web: an alert announces the newest event that is not an info line (a warning, a critical event or a
  recovery) instead of the latest info line.
- Web: charts count spend added after a budget reset instead of showing a negative or missing change.
- Web: a chart tooltip no longer comes back every poll after you click the chart, stays behind when the
  pointer leaves during a glide, or flashes while it glides.
- Web: the table view keeps its scroll position when new data arrives.
- Web: a chart whose range holds one poll says to pick a longer range instead of waiting forever.
- Web: a request to the keymeter server gives up when the answer, or the next part of its body, takes more
  than 5 seconds, or when any minute of it brings less than 60 KB, so a stalled or trickling connection no
  longer freezes the dashboard.
- Web: web fonts no longer hold up the first paint, and long histories draw faster.
- Web: chart tooltips are announced to screen readers, and meters report values from 0 to 100.
- Terminal: the live dashboard fits the window instead of losing its last lines. Long model and team-key
  tables end with "… and N more", and on a short window the table rows, then older events, the limits
  row and the footer give way before the newest event does.
- Terminal: control characters such as ESC are removed from gateway text (model names, aliases, error
  messages, events) before it is printed, so a gateway can't clear the screen or change the window title.
- Terminal: odd gateway values no longer crash the dashboard: an infinite or NaN rate limit or spend, a
  null or a number among the allowed models, a number instead of a list of them, or a numeric team-key
  alias. One model name instead of a list shows as that name (it was split into letters).
- Terminal: values wrap instead of being cut with "…".
- Terminal: a run-out time more than a week away shows as a duration only (a far-off one crashed the
  dashboard).
- Terminal: an overdue reset shows "resets in 0s" instead of a negative time.
- Terminal: says "team info unavailable" and why when `/team/info` fails (on one line live, in full with
  `--once`), and keeps saying it after a 401 or 403, when keymeter stops asking.
- Terminal: gateway text with square brackets shows as is instead of being read as markup.
- Terminal: a negative budget no longer crashes the budget bar.

## 0.1.0 (2026-10-07)

First public release.

- `keymeter web`: browser dashboard with spend vs. budget, burn rate, run-out time, projected spend at
  reset, spend-over-time charts, spend per model, limits, an event log, and optional sound and desktop
  alerts. Listens on localhost only unless `--host` says otherwise.
- `keymeter tui`: the same numbers as a live terminal dashboard, plus the team's budget and keys on
  LiteLLM when the key may read them.
- `keymeter json`: one snapshot as JSON, exit code 1 when the key is not usable.
- Gateways: LiteLLM proxy (`/key/info`) and OpenRouter (`/api/v1/key`), picked automatically from the
  URL or the key.

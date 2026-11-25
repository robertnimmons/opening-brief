# Opening Brief

A premarket briefing for the first hour of the open. Every weekday at about 8:30 AM Eastern, a GitHub Action (started by an external scheduler, see below) checks the watchlist's daily 8 EMA trend, premarket gaps, levels and news, then posts a short brief to Discord with the 1 or 2 names worth watching.

It never places trades and gives no buy or sell signals. All numbers come from code. Claude only writes the short reasons, and a validator removes any number it writes that isn't in the table.

## What the brief contains

1. **Header**: date, market bias (SPY and QQQ daily 8 EMA) and the index premarket gaps
2. **Focus**: 1 or 2 names in the direction of the market trend, each with gap, RS, premarket high/low, 8 EMA and one line of reasoning
3. **Watchlist table**: all tickers sorted by relative strength vs SPY
4. **Flags**: earnings this week, heavy news, 8 EMA coils, unavailable tickers
5. **Headlines**: the 2 or 3 that matter
6. A reminder that premarket levels are as of run time and should be rechecked on the chart at 9:15

## How the numbers are defined

| Item | Definition |
|---|---|
| Daily bars | Completed sessions only. Yahoo's partial row for today is dropped. `Close` is split-adjusted but not dividend-adjusted, matching what charts show. |
| 8 EMA | Seeded with the SMA of the first 8 closes, then `ema = close * 2/9 + prev * 7/9`. Matches TradingView's `ta.ema(close, 8)` on the daily chart. |
| Holding above / rejected below / chop | Last 3 daily closes all above the 8 EMA / all below / mixed (`status_lookback`). |
| 8 EMA coil | The longest 2 to 4 day window ending yesterday where every close sits within 0.5 ATR(14) of the 8 EMA and the whole window's high-low range is at most 1.5 ATR. |
| Premarket high/low | Max high and min low of 1-minute bars from 4:00 ET up to the run time. |
| Gap % | Last premarket price vs the prior session close. |
| RS vs SPY | Ticker gap % minus SPY gap %, in percentage points. |
| Market bias | Bullish if SPY and QQQ are both holding above their 8 EMA, Bearish if both are below, otherwise Mixed. |
| Focus candidates | Bullish: names holding above the 8 EMA (or coiling on it from above) with RS at least +0.3, strongest first. Bearish: the mirror image. Mixed: both sides, ranked by absolute RS. |
| Heavy news | At least 8 headlines in 24h and at least 2.5x the watchlist median. |

If a ticker's latest daily bar isn't the previous trading day (for example, Yahoo lagging), its gap is left blank and flagged instead of being computed against the wrong close.

## The agent

When `ANTHROPIC_API_KEY` is set, a Claude tool-use loop runs after the analysis. It has four tools: `get_company_news`, `get_earnings_calendar`, `get_price_history` and `get_market_news`. It looks into unusual gaps, heavy news and sector moves, then picks the final focus list from the rule-based candidates.

Guardrails enforced in code:

- At most 10 tool calls and 3 minutes. Requests past the cap are refused, and the model is told to answer. If it runs out of time, the brief goes out with the rule-based picks and a note.
- Picks outside the candidate list are rejected.
- Any number in its text has to be a correct rounding of a value in the table or a headline. Otherwise the reason is replaced with the rule-based reason (and an invented bias note is dropped).
- Headlines are chosen by id, so it can't make one up.
- Every step, tool call, tool result and validation problem is written to `logs/agent/YYYY-MM-DD.jsonl`.

Set `agent = false` in `config.toml` to use one plain summary call instead. With no Anthropic key, the brief goes out with the rule-based focus list and headlines.

## Setup

### 1. Create the private repo

```bash
cd opening-brief
gh repo create opening-brief --private --source . --push
```

Or create an empty private repo on github.com, then `git remote add origin <url>` and `git push -u origin main`.

### 2. Get the keys

- **Discord webhook**: in your server, open Server Settings > Integrations > Webhooks > New Webhook, pick the channel, then Copy Webhook URL.
- **Finnhub**: sign up for the free key at finnhub.io.
- **Anthropic**: optional. In the Claude Console go to Settings > API keys > Create key. Scope it to a workspace (for example the Default Workspace) so you don't need `ANTHROPIC_WORKSPACE_ID`. Personal keys (`sk-ant-usr...`) and service account keys both work.

### 3. Add them as GitHub Secrets

Go to the repo's Settings > Secrets and variables > Actions > New repository secret, and add:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the webhook URL |
| `FINNHUB_API_KEY` | your Finnhub key |
| `ANTHROPIC_API_KEY` | your Anthropic key (optional) |
| `ANTHROPIC_WORKSPACE_ID` | only if your key is not scoped to a workspace (`wrkspc_...`, from Console > Workspaces) |

The keys are only read from environment variables and are never written to the repo.

### 4. Allow the workflow to commit logs

Go to Settings > Actions > General > Workflow permissions and select **Read and write permissions**.

### 5. Test it

Go to Actions > Opening Brief > Run workflow and check **dry_run**. The full message is printed in the job log. Run it again without dry_run to send it to Discord. Manual runs skip the time window.

Then run **Actions > Verify numbers** once between 8:00 and 9:30 ET on a trading day. It runs the unit tests, then recomputes prior close, 8 EMA, gap and premarket high/low for NVDA, AAPL and SPY through a separate yfinance path and pandas. It prints both values side by side and fails if they disagree. Spot-check the 8 EMA against TradingView too.

## Running locally

```bash
pip install -r requirements.txt
export FINNHUB_API_KEY=...  ANTHROPIC_API_KEY=...        # optional for a dry run
python -m opening_brief --dry-run --force                # print today's brief now
python -m opening_brief --dry-run --date 2026-11-17      # replay a past day as of 8:30 ET
python -m opening_brief.verify NVDA AAPL SPY             # independent number check
python -m unittest discover -s tests -t .                # 43 tests, about 5 seconds
```

`--force` skips the time window and the already-sent check. `--no-llm` skips Claude.

Replay notes: Yahoo only serves 1-minute bars for about the last 30 days. For older dates, gaps fall back to the official open and the message says so. News for a replayed day is whatever Finnhub returns for that window.

## Changing things

- **Watchlist**: edit `watchlist` in `config.toml`. SPY and QQQ are always kept because the bias and RS depend on them. Index-type tickers in `index_tickers` show in the table but are never picked.
- **Run time**: set `target_time`, `window_start` and `window_end` in `config.toml`, then shift the cron lines in `.github/workflows/brief.yml` (UTC) so the first one lands about 25 minutes before the target in both EDT (UTC-4) and EST (UTC-5).
- **Thresholds**: everything in the `[analysis]` section, including EMA length, coil tolerances, minimum RS for a focus pick and heavy-news thresholds.
- **Model**: `model` under `[llm]`.
- **One-off closures**: add dates to `extra_closures`. Standard NYSE holidays (including Good Friday and observed weekend rules) are computed automatically.

## Scheduling details

The brief should go out once per weekday between 8:25 and 9:20 ET (`window_start` and `window_end` in `config.toml`). Two things start the workflow:

1. **The external scheduler (main path).** cron-job.org calls GitHub's API at 8:28 ET with `scheduled: true`. The run starts within a minute and sends right away. Setup is in the next section.
2. **GitHub's own cron lines (backup only).** The workflow still has slots at about 8:05, 8:20, 8:45 and 9:05 ET, but GitHub's `schedule` trigger is best effort and can't be relied on here. On Nov 24, 2026, the first day it fired at all, every slot started 9 to 10 hours late (5:15 to 6:30 PM ET) and exited with "Outside the run window". If a slot ever does land on time, a run that starts before 8:25 waits until 8:30 (`target_time`), and a run inside the window sends right away.

Whichever run sends first commits that day's log. Runs are serialized, and every later run that day sees the log and exits, so you get one brief per day. The script also exits on weekends and NYSE holidays. Half days (like the day after Thanksgiving) still open at 9:30, so the brief runs.

Manual runs from the Actions tab (Run workflow with `scheduled` unchecked) skip the time window and the already-sent check.

## Reliable scheduling with an external trigger

GitHub's own `schedule` trigger is best effort (see above). The dependable path is an outside scheduler that calls GitHub's API at 8:28 ET. That's the same mechanism as clicking Run workflow, with `scheduled: true` so the time window and the once-per-day check still apply.

1. **Create a token.** On GitHub, go to Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token.
   - Repository access: Only select repositories > `opening-brief`.
   - Permissions: Actions: Read and write. Leave everything else at no access.
   - Pick an expiration and note the date, since the trigger stops when the token expires.
2. **Create the job.** Make a free account at cron-job.org, then Create cronjob:
   - URL: `https://api.github.com/repos/robertnimmons/opening-brief/actions/workflows/brief.yml/dispatches`
   - Schedule: custom, weekdays (Mon-Fri) at 08:28, time zone America/New_York.
   - Advanced > Request method: POST.
   - Headers: `Authorization: Bearer <your token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`.
   - Request body: `{"ref":"main","inputs":{"scheduled":"true"}}`
3. **Test it** with the job's Test run button. GitHub answers `204 No Content`, and a new run appears under Actions with the event `workflow_dispatch`. Outside 8:25 to 9:20 ET that run exits with "Outside the run window", which is fine and proves the trigger works. A `401` means the token is wrong or expired, `404` means the token can't see the repo or the URL has a typo, and `422` usually means the body is malformed.
4. **Check the next weekday morning.** Look for a `workflow_dispatch` run that started around 8:28 ET and a brief in Discord. cron-job.org's job history also shows each call and GitHub's response.

Optional: add a second job at 08:45 as a backup. If the first one already sent, the second sees the log and exits.

## Logs

- `logs/brief_log.csv`: one row per ticker per day with every computed value, the candidate list, the focus picks and where they came from (agent, summary or rules). Use this later to check whether focus picks moved more than the rest.
- `logs/agent/YYYY-MM-DD.jsonl`: the agent's full trace. Dry-run traces get a `-dryrun` suffix and aren't committed.

## Failure behavior

| Situation | What happens |
|---|---|
| One ticker fails | One retry after 2s. If it still fails, its row says `unavailable` and the brief still sends. |
| Yahoo is down | The brief still sends, says price data is unavailable, and gives no focus list. |
| Finnhub key missing or Finnhub down | Price analysis sends. News says unavailable. |
| Anthropic key missing or API error | Rule-based focus and headlines, with a note if the AI step failed. |
| Discord webhook missing | Exit code 2 with a clear message. Nothing is logged. |
| Discord post fails | Exit code 1, so the Actions run shows red. |
| Holiday or weekend | Prints the reason and exits 0. |

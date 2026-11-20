# Opening Brief

A premarket briefing for the first hour of the open. Every weekday at about 8:30 AM Eastern, a GitHub Action checks the watchlist's daily 8 EMA trend, premarket gaps, levels and news, then posts a short brief to Discord with the 1 or 2 names worth watching.

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
- **Anthropic**: create a key at console.anthropic.com. This one is optional.

### 3. Add them as GitHub Secrets

Go to the repo's Settings > Secrets and variables > Actions > New repository secret, and add:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the webhook URL |
| `FINNHUB_API_KEY` | your Finnhub key |
| `ANTHROPIC_API_KEY` | your Anthropic key (optional) |

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
python -m unittest discover -s tests -t .                # 40 tests, about 4 seconds
```

`--force` skips the time window and the already-sent check. `--no-llm` skips Claude.

Replay notes: Yahoo only serves 1-minute bars for about the last 30 days. For older dates, gaps fall back to the official open and the message says so. News for a replayed day is whatever Finnhub returns for that window.

## Changing things

- **Watchlist**: edit `watchlist` in `config.toml`. SPY and QQQ are always kept because the bias and RS depend on them. Index-type tickers in `index_tickers` show in the table but are never picked.
- **Run time**: change the cron lines in `.github/workflows/brief.yml` (they're in UTC, two per season) and `window_start` / `window_end` in `config.toml` to match. For example, for 8:45 ET use `42 12` and `52 12` (EDT) plus `42 13` and `52 13` (EST), with a window of 08:40 to 09:05.
- **Thresholds**: everything in the `[analysis]` section, including EMA length, coil tolerances, minimum RS for a focus pick and heavy-news thresholds.
- **Model**: `model` under `[llm]`.
- **One-off closures**: add dates to `extra_closures`. Standard NYSE holidays (including Good Friday and observed weekend rules) are computed automatically.

## Scheduling details

GitHub's cron often runs 5 to 20 minutes late. The workflow fires twice per season (8:27 and 8:37 ET) so one of them lands inside the 8:25 to 8:50 window. Runs are serialized, and the second run sees the first run's log commit and exits, so you get one brief per day. The script also exits on weekends and NYSE holidays. Half days (like the day after Thanksgiving) still open at 9:30, so the brief runs.

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

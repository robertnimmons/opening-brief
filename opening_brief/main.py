"""Command line entry point: python -m opening_brief [--dry-run] [--date YYYY-MM-DD]."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time

from . import analysis as an
from .config import Secrets, load_config
from .data import YFinanceProvider, load_market_data
from .delivery import post_to_discord
from .formatting import BriefMeta, build_blocks, split_messages
from .llm import AnthropicClient, LLMOutcome, RunLog, ToolBox, run_agent, run_summary
from .logbook import already_logged, append_log
from .market_calendar import ET, closure_reason, in_window, now_et
from .news import FinnhubClient, apply_news_flags, fetch_news
from .util import log, setup_logging

EXIT_OK, EXIT_SEND_FAILED, EXIT_CONFIG = 0, 1, 2


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="opening_brief", description="Premarket watchlist brief")
    p.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    p.add_argument("--date", help="replay a past day (YYYY-MM-DD) as of the configured replay time")
    p.add_argument("--force", action="store_true",
                   help="skip the 8:25-8:50 ET window and the already-sent check")
    p.add_argument("--no-llm", action="store_true", help="skip the Claude step")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def run(argv=None, *, provider=None, finnhub_session=None, anthropic_client=None,
        now: datetime | None = None, poster=post_to_discord, secrets: Secrets | None = None,
        out=print) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    secrets = secrets or Secrets.from_env()
    sched, acfg = cfg.schedule, cfg.analysis
    extra = sched.get("extra_closures", [])

    # ---------------------------------------------------------- when
    replay = bool(args.date)
    if replay:
        try:
            brief_date = date.fromisoformat(args.date)
        except ValueError:
            out(f"Bad --date {args.date!r}; use YYYY-MM-DD")
            return EXIT_CONFIG
        hh, mm = (int(x) for x in sched.get("replay_as_of", "08:30").split(":"))
        as_of = datetime.combine(brief_date, time(hh, mm), tzinfo=ET)
    else:
        as_of = (now or now_et()).astimezone(ET)
        brief_date = as_of.date()

    reason = closure_reason(brief_date, extra)
    if reason:
        out(f"Market closed on {brief_date} ({reason}). Nothing to send.")
        return EXIT_OK

    if not replay and not args.force and not in_window(
            as_of, sched.get("window_start", "08:25"), sched.get("window_end", "08:50")):
        out(f"Outside the run window ({as_of:%H:%M} ET). Exiting without sending.")
        return EXIT_OK

    if not args.dry_run and not secrets.discord:
        out("DISCORD_WEBHOOK_URL is not set. Add it as a GitHub secret, or use --dry-run to print.")
        return EXIT_CONFIG

    log_csv = cfg.log_dir / "brief_log.csv"
    live = not replay and not args.dry_run
    if live and not args.force and already_logged(log_csv, brief_date):
        out(f"Brief for {brief_date} was already sent. Exiting.")
        return EXIT_OK

    # ---------------------------------------------------------- data + analysis
    log.info("Building brief for %s as of %s ET", brief_date, as_of.strftime("%H:%M"))
    provider = provider or YFinanceProvider()
    data = load_market_data(provider, cfg.watchlist, brief_date, as_of, replay=replay)
    a = an.build_analysis(data, brief_date, acfg, cfg.index_tickers, extra)

    finnhub = FinnhubClient(secrets.finnhub, session=finnhub_session)
    bundle = fetch_news(finnhub, cfg.watchlist, as_of, cfg.news.get("lookback_hours", 24), brief_date)
    apply_news_flags(a, bundle, acfg)
    an.apply_rule_focus(a, acfg)

    # ---------------------------------------------------------- LLM
    llm = LLMOutcome()
    max_focus = acfg.get("max_focus", 2)
    use_llm = cfg.llm.get("enabled", True) and not args.no_llm
    if use_llm and not secrets.anthropic and anthropic_client is None:
        log.info("ANTHROPIC_API_KEY not set; sending the computed brief without the AI summary")
    elif use_llm:
        client = anthropic_client or AnthropicClient(
            secrets.anthropic, cfg.llm.get("model", "claude-sonnet-5-5"),
            cfg.llm.get("max_tokens", 1500))
        suffix = "" if live else "-dryrun"
        runlog = RunLog(cfg.log_dir / "agent" / f"{brief_date.isoformat()}{suffix}.jsonl")
        if cfg.llm.get("agent", True):
            tools = ToolBox(a, bundle, data, finnhub, as_of, cfg.news.get("lookback_hours", 24),
                            acfg.get("ema_length", 8))
            llm = run_agent(client, tools, a, bundle, max_focus, runlog,
                            max_calls=cfg.llm.get("max_tool_calls", 10),
                            time_limit=cfg.llm.get("time_limit_seconds", 180))
        else:
            llm = run_summary(client, a, bundle, max_focus, runlog)
        if llm.ok:
            an.set_focus(a, llm.focus, llm.reasons, llm.mode)
        for p in llm.problems:
            log.warning("LLM output issue: %s", p)

    # ---------------------------------------------------------- deliver
    blocks = build_blocks(a, bundle, llm, BriefMeta(as_of=as_of, replay=replay),
                          cfg.news.get("headlines_in_brief", 3))
    messages = split_messages(blocks, cfg.delivery.get("discord_char_limit", 2000))

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            out(f"----- message {i}/{len(messages)} ({len(m)} chars) -----")
            out(m)
        return EXIT_OK

    try:
        poster(secrets.discord, messages)
    except Exception as exc:  # noqa: BLE001
        out(f"Sending to Discord failed: {exc}")
        return EXIT_SEND_FAILED

    if live and already_logged(log_csv, brief_date):
        log.info("A brief for %s is already in the log; not logging this forced re-run", brief_date)
    elif live:
        n = append_log(log_csv, a, as_of)
        log.info("Logged %d rows to %s", n, log_csv)
    return EXIT_OK


def main() -> None:
    sys.exit(run())

"""Render the brief as Discord messages that read well on a phone.

Every number shown here is formatted straight from the analysis table.
LLM text appears only as the short reason lines, the bias note and the
choice of headlines.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .analysis import STATUS_CODE, Analysis, Row, direction_of
from .llm import LLMOutcome
from .news import Headline, NewsBundle


@dataclass
class BriefMeta:
    as_of: datetime
    replay: bool = False


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _pct(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{nd}f}"


def _flags(r: Row) -> str:
    f = ""
    if r.consolidation_days:
        f += "c"
    if r.earnings:
        f += "E"
    if r.heavy_news:
        f += "N"
    return f


def table_lines(a: Analysis) -> list[str]:
    rows = a.ranked()
    stocks = [r for r in rows if not r.is_index]
    idx = [r for r in rows if r.is_index]

    def pm(r: Row) -> str:
        if r.pm_high is None:
            return "n/a"
        return f"{_p(r.pm_high)}/{_p(r.pm_low)}"

    pm_w = max([len(pm(r)) for r in rows if r.available] + [9])
    header = f"{'TKR':<6}{'GAP%':>5}{'RS':>6} {'8EMA':<5}{'PM HI/LO':<{pm_w}} "
    out = [header.rstrip()]

    def line(r: Row) -> str:
        name = r.ticker + ("*" if r.is_focus else "")
        if not r.available:
            return f"{name:<6}unavailable"
        return (f"{name:<6}{_pct(r.gap_pct):>5}{_pct(r.rs_vs_spy):>6} "
                f"{STATUS_CODE[r.status]:<5}{pm(r):<{pm_w}} {_flags(r)}").rstrip()

    out += [line(r) for r in stocks]
    if idx:
        out.append("-" * len(header.rstrip()))
        out += [line(r) for r in idx]
    return out


def pick_headlines(a: Analysis, bundle: NewsBundle, llm: LLMOutcome, n: int) -> list[Headline]:
    pool = {h.id: h for h in bundle.all}
    pool.update({h.id: h for h in (bundle.market or [])})
    if llm.ok and llm.headline_ids:
        return [pool[i] for i in llm.headline_ids if i in pool][:n]
    # deterministic fallback: newest headline for focus names, then heavy-news
    # names, then earnings-today names, then the most cross-listed headlines
    chosen: list[Headline] = []

    def add(h: Headline | None):
        if h and h not in chosen and len(chosen) < n:
            chosen.append(h)

    for t in a.focus:
        add(next(iter(bundle.by_ticker.get(t, [])), None))
    for r in a.ranked():
        if r.heavy_news or r.earnings_today:
            add(next(iter(bundle.by_ticker.get(r.ticker, [])), None))
    for h in sorted(bundle.all, key=lambda h: len(h.tickers), reverse=True):
        add(h)
    return chosen


def _md_escape(text: str) -> str:
    for ch in ("*", "_", "~", "`", "|", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


def build_blocks(a: Analysis, bundle: NewsBundle, llm: LLMOutcome, meta: BriefMeta,
                 headlines_n: int = 3) -> list[str]:
    """Return message blocks in reading order. Each block is kept whole when splitting."""
    blocks: list[str] = []
    d = a.brief_date
    title = f"**Opening Brief · {d:%a %b} {d.day}**"
    if meta.replay:
        title += f"  (replay, as of {meta.as_of:%H:%M} ET)"
    idx_bits = []
    for t in ("SPY", "QQQ", "IWM"):
        r = a.rows.get(t)
        if r is not None:
            idx_bits.append(f"{t} {_pct(r.gap_pct, 2)}%" if r.available and r.gap_pct is not None
                            else f"{t} n/a")
    head = [title, f"Bias: **{a.bias}** · {a.bias_detail}"]
    if idx_bits:
        head.append("Premarket: " + " · ".join(idx_bits))
    if llm.ok and llm.bias_note:
        head.append(f"_{llm.bias_note}_")
    blocks.append("\n".join(head))

    # Focus
    src = "agent pick" if (llm.ok and a.focus_source == "agent") else \
          "summary pick" if (llm.ok and a.focus_source == "summary") else "rule pick"
    f_lines = [f"**Focus** ({src})"]
    if not a.focus:
        reason = (llm.note if llm.ok and llm.note else
                  "No trend-aligned name cleared the relative strength bar today."
                  if a.bias != "Unknown" else "Market bias unavailable, so no focus list.")
        f_lines.append(reason)
    for i, t in enumerate(a.focus, 1):
        r = a.rows[t]
        f_lines.append(
            f"**{i}. {t}** · {direction_of(r)} · gap {_pct(r.gap_pct, 2)}% · RS {_pct(r.rs_vs_spy, 2)}")
        f_lines.append(f"PM H {_p(r.pm_high)} / L {_p(r.pm_low)} · 8EMA {_p(r.ema8)}")
        f_lines.append(f"> {r.focus_reason}")
    blocks.append("\n".join(f_lines))

    # Table (kept as its own block; the splitter can break it by rows)
    blocks.append("TABLE::" + "\n".join(table_lines(a)))

    # Flags
    fl = ["_ABV/BLW/CHP: above, below, chop on daily 8EMA · c coil · E earnings · N heavy news · * focus_"]
    earn = [f"{r.ticker} {r.earnings}" for r in a.ranked() if r.earnings]
    if earn:
        fl.append("Earnings: " + ", ".join(earn))
    elif not bundle.earnings_available:
        fl.append("Earnings: calendar unavailable")
    heavy = [f"{r.ticker} ({r.news_count})" for r in a.ranked() if r.heavy_news]
    if heavy:
        fl.append("Heavy news: " + ", ".join(heavy))
    coil = [f"{r.ticker} {r.consolidation_days}d" for r in a.ranked() if r.consolidation_days]
    if coil:
        fl.append("8EMA coil: " + ", ".join(coil))
    down = [r for r in a.ranked() if not r.available]
    if down and len(down) == len(a.rows):
        fl.append("Price data unavailable for every ticker (Yahoo may be down). Check the charts directly.")
    else:
        by_err: dict[str, list[str]] = {}
        for r in down:
            by_err.setdefault(r.error or "no data", []).append(r.ticker)
        for err, ts in by_err.items():
            fl.append(f"Unavailable ({err}): " + ", ".join(ts))
    no_pm = [r.ticker for r in a.ranked() if r.available and r.gap_pct is None]
    if no_pm:
        fl.append("No premarket data: " + ", ".join(no_pm))
    stale = [f"{r.ticker} ({r.stale_note})" for r in a.ranked() if r.stale_note]
    if stale:
        fl.append("Check prior close: " + "; ".join(stale))
    blocks.append("\n".join(fl))

    # News
    n_lines = ["**Headlines**"]
    if not bundle.available:
        n_lines.append(f"News unavailable ({bundle.error}).")
    else:
        picks = pick_headlines(a, bundle, llm, headlines_n)
        if not picks:
            n_lines.append("No watchlist headlines in the last 24h.")
        for h in picks:
            who = ", ".join(h.tickers[:3]) if h.tickers else "Market"
            if len(h.tickers) > 3:
                who += f" +{len(h.tickers) - 3}"
            text = _md_escape(h.headline)
            link = f"[{text}](<{h.url}>)" if h.url else text
            n_lines.append(f"• **{who}** {link} · {h.source}")
    blocks.append("\n".join(n_lines))

    # Footer
    foot = []
    if not any(r.pm_high is not None or r.gap_source == "open" for r in a.rows.values()):
        foot.append("_No premarket levels available. Mark them on the chart at 9:15._")
    elif any(r.gap_source == "open" for r in a.rows.values()):
        foot.append("_Premarket bars unavailable for this date; gaps use the official open._")
    else:
        hh = meta.as_of.hour % 12 or 12
        foot.append(f"_Premarket levels as of {hh}:{meta.as_of:%M} ET. Check them on the chart at 9:15._")
    if llm.used and not llm.ok:
        why = {"time_cap": "hit its time cap", "tool_cap": "hit its tool cap",
               "error": "failed"}.get(llm.stopped_by or "", "returned no usable answer")
        foot.append(f"_AI summary {why}; focus list and headlines are rule-based._")
    elif llm.used and llm.mode == "agent":
        foot.append(f"_Agent used {llm.tool_calls} tool call{'s' if llm.tool_calls != 1 else ''}._")
    foot.append("_Not trade advice._")
    blocks.append("\n".join(foot))
    return blocks


def _code(lines: list[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


def split_messages(blocks: list[str], limit: int = 2000) -> list[str]:
    """Pack blocks into messages no longer than `limit`, never splitting inside a
    block except the table, which is split by rows with its header repeated."""
    pieces: list[str] = []
    for b in blocks:
        if b.startswith("TABLE::"):
            lines = b[len("TABLE::"):].split("\n")
            header, body = lines[0], lines[1:]
            chunk: list[str] = []
            for ln in body:
                if len(_code([header] + chunk + [ln])) > limit and chunk:
                    pieces.append(_code([header] + chunk))
                    chunk = []
                chunk.append(ln)
            pieces.append(_code([header] + chunk))
        elif len(b) <= limit:
            pieces.append(b)
        else:  # very long prose block: split on lines
            cur = ""
            for ln in b.split("\n"):
                while len(ln) > limit:
                    pieces.append(ln[:limit])
                    ln = ln[limit:]
                if len(cur) + len(ln) + 1 > limit:
                    pieces.append(cur)
                    cur = ln
                else:
                    cur = f"{cur}\n{ln}" if cur else ln
            if cur:
                pieces.append(cur)

    messages: list[str] = []
    cur = ""
    for p in pieces:
        cand = f"{cur}\n\n{p}" if cur else p
        if len(cand) <= limit:
            cur = cand
        else:
            messages.append(cur)
            cur = p
    if cur:
        messages.append(cur)
    return messages

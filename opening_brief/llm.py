"""Claude summary and tool-use agent.

The LLM never produces numbers that reach the table: the table, levels and
gaps are rendered from code. The LLM only writes short reasons, a bias note,
and chooses headline ids and focus tickers. Everything it returns is
validated here:

* focus tickers must come from the rule-based candidate list
* headline ids must exist
* every number in its text must be a correct rounding of a value that is in
  the analysis table or in a headline; otherwise that text is replaced with
  the deterministic version and the violation is logged
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

from .analysis import STATUS_TEXT, Analysis, direction_of, rule_reason
from .indicators import ema
from .news import FinnhubClient, NewsBundle, fetch_market_news
from .util import log

API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------- client


class AnthropicClient:
    def __init__(self, api_key: str, model: str, max_tokens: int = 1500,
                 session: requests.Session | None = None, backoff: float = 3.0):
        self.key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.s = session or requests.Session()
        self.backoff = backoff

    def create(self, *, system: str, messages: list, tools: list | None = None,
               tool_choice: dict | None = None, timeout: float = 60.0) -> dict:
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "system": system, "messages": messages}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        for attempt in (1, 2):
            try:
                r = self.s.post(API_URL, headers=headers, json=body, timeout=timeout)
                if r.status_code in (429, 500, 502, 503, 529) and attempt == 1:
                    log.warning("Anthropic API %s; retrying once", r.status_code)
                    time.sleep(self.backoff)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
                return r.json()
            except requests.RequestException as exc:
                if attempt == 2:
                    raise
                log.warning("Anthropic request failed (%s); retrying once", exc)
                time.sleep(self.backoff)
        raise RuntimeError("Anthropic API unreachable")


# ---------------------------------------------------------------- context


def _fmt(x, nd=2, sign=False):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def table_for_llm(a: Analysis) -> str:
    lines = ["ticker | prior_close | ema8 | ema8_status | coil_days | pm_high | pm_low | pm_last | "
             "gap_pct | rs_vs_spy | rank | news_24h | heavy_news | earnings"]
    for r in a.ranked():
        if not r.available:
            lines.append(f"{r.ticker} | UNAVAILABLE ({r.error})")
            continue
        lines.append(" | ".join([
            r.ticker + (" (index)" if r.is_index else ""),
            _fmt(r.prior_close), _fmt(r.ema8), STATUS_TEXT[r.status], str(r.consolidation_days),
            _fmt(r.pm_high), _fmt(r.pm_low), _fmt(r.pm_last),
            _fmt(r.gap_pct, sign=True) + ("" if r.gap_source != "open" else " (from open)"),
            _fmt(r.rs_vs_spy, sign=True), str(r.rank or "-"), str(r.news_count),
            "yes" if r.heavy_news else "no", r.earnings or "-",
        ]))
    return "\n".join(lines)


def headlines_for_llm(bundle: NewsBundle, limit: int = 60) -> str:
    if not bundle.available:
        return "(company news unavailable)"
    rows = [f"{h.id} [{','.join(h.tickers)}] {h.headline} ({h.source}, {h.published:%a %H:%M} ET)"
            for h in bundle.all[:limit]]
    return "\n".join(rows) or "(no headlines in the last 24h)"


def context_message(a: Analysis, bundle: NewsBundle, max_focus: int) -> str:
    cands = ", ".join(f"{t} ({direction_of(a.rows[t])})" for t in a.candidates) or "none"
    return (
        f"Brief date: {a.brief_date:%A %Y-%m-%d}\n"
        f"Market bias (from SPY/QQQ daily 8EMA, computed): {a.bias}. {a.bias_detail}\n"
        f"Rule-based focus list: {', '.join(a.focus) or 'none'}\n"
        f"CANDIDATES you may choose from (max {max_focus}): {cands}\n\n"
        f"ANALYSIS TABLE (computed by code; gap and RS are percent / percentage points):\n"
        f"{table_for_llm(a)}\n\nHEADLINES (last 24h, deduplicated):\n{headlines_for_llm(bundle)}"
    )


FINAL_SPEC = """Finish with ONLY a JSON object, no prose around it:
{"bias_note": "<one sentence on market tone, citing table values only>",
 "focus": [{"ticker": "<from CANDIDATES>", "reason": "<one sentence, max 25 words>"}],
 "headline_ids": ["<2 or 3 ids from the headline lists that matter most today>"],
 "note": "<optional one sentence about anything unusual, or empty string>"}
focus may hold 0, 1 or 2 entries. Use 0 only if no candidate is worth attention, and say why in note."""

RULES = """Hard rules:
- Do not compute, estimate, round differently, or invent any number. Only cite numbers exactly as they appear in the ANALYSIS TABLE or inside headline text. Do not cite numbers from price history; use it only to understand context.
- No trade advice: no buy/sell/short calls, entries, stops, targets, or position sizing. Describe what stands out, not what to do.
- Focus tickers must come from CANDIDATES. They are already aligned with the SPY/QQQ daily trend.
- Be brief and plain. No hype."""

AGENT_SYSTEM = f"""You prepare a trader's 9:00 AM premarket brief. Code has already computed every number. Your job is to investigate what stands out and pick the 1 or 2 names that deserve attention at the open.

Investigate, within budget: unusual gaps (abs gap or abs RS around 2 or more), names with heavy news or earnings today/this week, and sector moves (for example semis NVDA AMD MU AVGO INTC moving together, or crypto-linked COIN HOOD). Use tools only when they can change your pick. You have at most {{max_calls}} tool calls in total and about 3 minutes. Before each tool call write one short sentence saying why.

{RULES}

{FINAL_SPEC}"""

SUMMARY_SYSTEM = f"""You turn a computed premarket table and headlines into a short brief for a trader. Pick up to the allowed number of focus names from CANDIDATES with one line of reasoning each, and the 2 or 3 headlines that matter.

{RULES}

{FINAL_SPEC}"""


# ---------------------------------------------------------------- number guard

NUM_RE = re.compile(r"(?<![A-Za-z])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
ALWAYS_OK = {float(i) for i in range(0, 11)} | {20.0, 50.0, 100.0, 200.0, 500.0, 2000.0} | \
    {float(y) for y in range(2020, 2036)}


def allowed_numbers(a: Analysis, bundle: NewsBundle) -> set[float]:
    vals: set[float] = set(ALWAYS_OK)
    fields = ("prior_close", "ema8", "pm_high", "pm_low", "pm_last", "gap_pct", "rs_vs_spy",
              "consolidation_days", "news_count", "rank", "atr14")
    for r in a.rows.values():
        for f in fields:
            v = getattr(r, f)
            if v is not None:
                vals.add(abs(float(v)))
    heads = list(bundle.all) + list(bundle.market or [])
    for h in heads:
        for tok in NUM_RE.findall(TIME_RE.sub(" ", h.headline)):
            v = _tok_value(tok)
            if v is not None:
                vals.add(abs(v))
    return vals


def _tok_value(tok: str) -> float | None:
    t = tok.replace("$", "").replace(",", "").replace("%", "").lstrip("+")
    try:
        return float(t)
    except ValueError:
        return None


def bad_numbers(text: str, allowed: set[float]) -> list[str]:
    """Tokens in text that are not a correct rounding of any allowed value."""
    bad = []
    for tok in NUM_RE.findall(TIME_RE.sub(" ", text or "")):
        v = _tok_value(tok)
        if v is None:
            continue
        core = tok.replace("$", "").replace(",", "").replace("%", "").lstrip("+-")
        decimals = len(core.split(".")[1]) if "." in core else 0
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        if not any(abs(abs(v) - a) <= tol for a in allowed):
            bad.append(tok)
    return bad


# ---------------------------------------------------------------- outcome


@dataclass
class LLMOutcome:
    used: bool = False
    mode: str = "none"                  # "agent" | "summary" | "none"
    ok: bool = False
    focus: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    bias_note: str | None = None
    note: str | None = None
    headline_ids: list[str] = field(default_factory=list)
    tool_calls: int = 0
    stopped_by: str | None = None       # "tool_cap" | "time_cap" | "error" | None
    problems: list[str] = field(default_factory=list)


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def validate(result: dict | None, a: Analysis, bundle: NewsBundle, max_focus: int,
             outcome: LLMOutcome) -> None:
    if not isinstance(result, dict):
        outcome.problems.append("final answer was not valid JSON")
        return
    allowed = allowed_numbers(a, bundle)

    focus, reasons = [], {}
    for item in result.get("focus") or []:
        if not isinstance(item, dict):
            continue
        t = str(item.get("ticker", "")).upper().strip()
        if t not in a.candidates:
            outcome.problems.append(f"rejected pick {t!r}: not in candidates")
            continue
        if t in focus or len(focus) >= max_focus:
            continue
        reason = str(item.get("reason", "")).strip()
        bad = bad_numbers(reason, allowed)
        if bad:
            outcome.problems.append(f"{t} reason cited numbers not in table {bad}; replaced with rule text")
            reason = rule_reason(a.rows[t])
        focus.append(t)
        reasons[t] = reason or rule_reason(a.rows[t])

    def clean(text_key: str) -> str | None:
        txt = str(result.get(text_key) or "").strip()
        if not txt:
            return None
        bad = bad_numbers(txt, allowed)
        if bad:
            outcome.problems.append(f"{text_key} cited numbers not in table {bad}; dropped")
            return None
        return txt

    valid_ids = {h.id for h in bundle.all} | {h.id for h in (bundle.market or [])}
    ids = []
    for hid in result.get("headline_ids") or []:
        hid = str(hid)
        if hid in valid_ids and hid not in ids:
            ids.append(hid)
        elif hid not in valid_ids:
            outcome.problems.append(f"unknown headline id {hid!r}")
    outcome.focus = focus
    outcome.reasons = reasons
    outcome.bias_note = clean("bias_note")
    outcome.note = clean("note")
    outcome.headline_ids = ids[:3]
    outcome.ok = True


# ---------------------------------------------------------------- agent tools

TOOLS = [
    {"name": "get_company_news",
     "description": "Deduplicated headlines from the last 24h for one watchlist ticker.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}},
                      "required": ["ticker"]}},
    {"name": "get_earnings_calendar",
     "description": "Earnings date and timing (BMO/AMC) for one watchlist ticker if it reports this week.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}},
                      "required": ["ticker"]}},
    {"name": "get_price_history",
     "description": "Recent completed daily bars (OHLCV and 8EMA) for one watchlist ticker. Context only: do not cite these numbers.",
     "input_schema": {"type": "object", "properties": {
         "ticker": {"type": "string"},
         "days": {"type": "integer", "minimum": 1, "maximum": 30}},
         "required": ["ticker", "days"]}},
    {"name": "get_market_news",
     "description": "General market headlines from the last 24h (macro, Fed, sector news).",
     "input_schema": {"type": "object", "properties": {}}},
]


class ToolBox:
    def __init__(self, a: Analysis, bundle: NewsBundle, daily: dict, finnhub: FinnhubClient,
                 as_of: datetime, lookback_hours: int, ema_length: int = 8):
        self.a, self.bundle, self.daily = a, bundle, daily
        self.finnhub, self.as_of, self.lookback = finnhub, as_of, lookback_hours
        self.ema_length = ema_length

    def _ticker(self, args) -> str:
        t = str(args.get("ticker", "")).upper().strip()
        if t not in self.a.rows:
            raise ValueError(f"{t!r} is not on the watchlist")
        return t

    def run(self, name: str, args: dict) -> str:
        if name == "get_company_news":
            t = self._ticker(args)
            if not self.bundle.available:
                return json.dumps({"error": "company news unavailable"})
            items = [{"id": h.id, "headline": h.headline, "source": h.source,
                      "published_et": h.published.strftime("%a %H:%M")}
                     for h in self.bundle.by_ticker.get(t, [])[:15]]
            return json.dumps({"ticker": t, "count_24h": len(self.bundle.by_ticker.get(t, [])),
                               "headlines": items})
        if name == "get_earnings_calendar":
            t = self._ticker(args)
            if not self.bundle.earnings_available:
                return json.dumps({"error": "earnings calendar unavailable"})
            ev = self.bundle.earnings.get(t)
            if not ev:
                return json.dumps({"ticker": t, "reports_this_week": False})
            return json.dumps({"ticker": t, "reports_this_week": True,
                               "date": ev.date.isoformat(), "timing": ev.hour or "unknown"})
        if name == "get_price_history":
            t = self._ticker(args)
            days = max(1, min(30, int(args.get("days", 10))))
            td = self.daily.get(t)
            if td is None or td.daily is None:
                return json.dumps({"error": f"no daily data for {t}"})
            d = td.daily
            e = ema(list(d["Close"]), self.ema_length)
            tail = d.tail(days)
            bars = [{"date": idx.date().isoformat(), "open": round(r.Open, 2), "high": round(r.High, 2),
                     "low": round(r.Low, 2), "close": round(r.Close, 2), "volume": int(r.Volume),
                     "ema8": round(e[len(d) - days + i], 2)}
                    for i, (idx, r) in enumerate(tail.iterrows())]
            return json.dumps({"ticker": t, "bars": bars})
        if name == "get_market_news":
            if self.bundle.market is None:
                try:
                    self.bundle.market = fetch_market_news(self.finnhub, self.as_of, self.lookback)
                except Exception as exc:  # noqa: BLE001
                    self.bundle.market = []
                    return json.dumps({"error": f"market news unavailable: {exc}"})
            return json.dumps({"headlines": [
                {"id": h.id, "headline": h.headline, "source": h.source,
                 "published_et": h.published.strftime("%a %H:%M")} for h in self.bundle.market]})
        raise ValueError(f"unknown tool {name}")


# ---------------------------------------------------------------- runners


class RunLog:
    """Append-only JSONL log of every LLM step for later review."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **data) -> None:
        if not self.path:
            return
        rec = {"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")


def _texts(content: list) -> str:
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def run_summary(client: AnthropicClient, a: Analysis, bundle: NewsBundle, max_focus: int,
                runlog: RunLog, time_limit: float = 60.0) -> LLMOutcome:
    out = LLMOutcome(used=True, mode="summary")
    ctx = context_message(a, bundle, max_focus)
    runlog.write("start", mode="summary", model=client.model, context=ctx)
    try:
        resp = client.create(system=SUMMARY_SYSTEM, messages=[{"role": "user", "content": ctx}],
                             timeout=time_limit)
    except Exception as exc:  # noqa: BLE001
        out.stopped_by, out.problems = "error", [str(exc)]
        runlog.write("error", error=str(exc))
        return out
    text = _texts(resp.get("content", []))
    runlog.write("final_text", text=text)
    validate(extract_json(text), a, bundle, max_focus, out)
    runlog.write("validated", focus=out.focus, reasons=out.reasons, problems=out.problems)
    return out


def run_agent(client: AnthropicClient, tools: ToolBox, a: Analysis, bundle: NewsBundle,
              max_focus: int, runlog: RunLog, max_calls: int = 10,
              time_limit: float = 180.0, clock=time.monotonic) -> LLMOutcome:
    out = LLMOutcome(used=True, mode="agent")
    deadline = clock() + time_limit
    system = AGENT_SYSTEM.replace("{max_calls}", str(max_calls))
    ctx = context_message(a, bundle, max_focus)
    messages: list = [{"role": "user", "content": ctx}]
    runlog.write("start", mode="agent", model=client.model, max_calls=max_calls,
                 time_limit=time_limit, context=ctx)

    final_text = None
    step = 0
    while True:
        remaining = deadline - clock()
        if remaining < 5:
            out.stopped_by = "time_cap"
            runlog.write("cap", which="time", tool_calls=out.tool_calls)
            break
        budget_left = out.tool_calls < max_calls
        step += 1
        try:
            resp = client.create(
                system=system, messages=messages, tools=TOOLS,
                tool_choice={"type": "auto"} if budget_left else {"type": "none"},
                timeout=min(60.0, remaining),
            )
        except Exception as exc:  # noqa: BLE001
            out.stopped_by = "error"
            out.problems.append(str(exc))
            runlog.write("error", step=step, error=str(exc))
            break
        content = resp.get("content", [])
        reasoning = _texts(content)
        uses = [b for b in content if b.get("type") == "tool_use"]
        runlog.write("assistant", step=step, stop_reason=resp.get("stop_reason"),
                     reasoning=reasoning, tool_requests=[{"name": u["name"], "input": u.get("input")} for u in uses])
        messages.append({"role": "assistant", "content": content})

        if not uses:
            final_text = reasoning
            break

        results = []
        for u in uses:
            if out.tool_calls >= max_calls:
                res, is_err = "Tool budget exhausted. Reply with the final JSON now.", True
                runlog.write("tool_refused", step=step, name=u["name"], input=u.get("input"))
            else:
                out.tool_calls += 1
                try:
                    res, is_err = tools.run(u["name"], u.get("input") or {}), False
                except Exception as exc:  # noqa: BLE001
                    res, is_err = json.dumps({"error": str(exc)}), True
                runlog.write("tool_call", step=step, n=out.tool_calls, name=u["name"],
                             input=u.get("input"), is_error=is_err, result=res[:4000])
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": res,
                            **({"is_error": True} if is_err else {})})
        if out.tool_calls >= max_calls:
            results.append({"type": "text", "text": "Tool budget used up. Reply with the final JSON now."})
            if out.stopped_by is None:
                out.stopped_by = "tool_cap"
                runlog.write("cap", which="tool_calls", tool_calls=out.tool_calls)
        messages.append({"role": "user", "content": results})

    if final_text is not None:
        runlog.write("final_text", text=final_text)
        validate(extract_json(final_text), a, bundle, max_focus, out)
    runlog.write("validated", ok=out.ok, focus=out.focus, reasons=out.reasons,
                 bias_note=out.bias_note, headline_ids=out.headline_ids,
                 problems=out.problems, tool_calls=out.tool_calls, stopped_by=out.stopped_by)
    return out

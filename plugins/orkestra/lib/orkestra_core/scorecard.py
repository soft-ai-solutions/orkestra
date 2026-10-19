"""Run scorecard: tempo, quality and cost figures of one run, derived from files that already exist.

Read-only and local: state.json (tasks, history, saved usage), the activity rows and the token numbers of the sessions
bound to the run (usage.py). Nothing is sent anywhere and nothing is stored. Used by `orkestra tempo` and by the
report's machine-evidence block.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import paths
from . import tempo as T

MARK = "<!-- orkestra:scorecard -->"       # ends every scorecard line in the evidence block (see evidence._strip_scorecard)
PRICE_KEYS = ("input", "output", "cache_write", "cache_read")


def _cfg() -> Dict[str, Any]:
    try:
        cfg = paths.global_config().get("scorecard")
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def cost_usd(usage: Optional[Dict[str, Any]], prices: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Estimated spend from token counts and `scorecard.price_usd_per_mtok` (USD per million tokens, per kind).
    None when no price is configured or no usage is known: a made-up price would be a false number."""
    prices = prices if prices is not None else _cfg().get("price_usd_per_mtok")
    if not isinstance(usage, dict) or not isinstance(prices, dict):
        return None
    total, priced = 0.0, False
    for key in PRICE_KEYS:
        try:
            price = float(prices.get(key))
        except (TypeError, ValueError):
            continue
        priced = True
        total += float(usage.get(key) or 0) * price / 1_000_000
    return total if priced else None


def warn_threshold() -> float:
    try:
        return max(0.0, float(_cfg().get("cost_warn_usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def _usage(project: Path, st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if isinstance(st.get("usage"), dict):
        return st["usage"]                                 # closed run: the numbers saved when it finished
    from . import runs as R                                # late import: runs is heavy
    return R.usage_snapshot(project, st)


def compute(project: Path, st: Dict[str, Any], rows: Optional[List[Dict[str, Any]]] = None,
            with_usage: bool = True) -> Dict[str, Any]:
    """The scorecard as data. Fields that cannot be known are None (or absent), never guessed."""
    tasks = st.get("tasks") or {}
    done = [t for t in tasks.values() if t.get("status") == "done"]
    reopens = sum(T.reopen_count(t) for t in tasks.values())
    created = T._ts(st.get("created"))
    reds = [T._ts((t.get("red") or {}).get("at")) for t in tasks.values()]
    reds = [x for x in reds if x]
    mix: Dict[str, int] = {}
    for t in tasks.values():
        why = (t.get("red") or {}).get("reason")
        if isinstance(why, str) and why:
            mix[why] = mix.get(why, 0) + 1
    regressions = sum(1 for h in (st.get("history") or [])
                      if str(h.get("note") or "").startswith("back:") and "regress" in str(h.get("note")).lower())
    card: Dict[str, Any] = {
        "tasks": len(tasks),
        "first_pass": sum(1 for t in done if not T.reopen_count(t)),
        "done": len(done),
        "first_pass_rate": (sum(1 for t in done if not T.reopen_count(t)) / len(done)) if done else None,
        "reopens": reopens,
        "reopens_per_task": (reopens / len(tasks)) if tasks else None,
        "red_reasons": mix,
        "regressions_caught": regressions,
        "time_to_first_red": (min(reds) - created) if reds and created and min(reds) >= created else None,
        "deferred": sum(1 for t in tasks.values() if t.get("status") == "deferred"),
        "blocked": sum(1 for t in tasks.values() if t.get("status") == "blocked"),
        "dead_wait_share": T.dead_wait_share(rows)["share"] if rows else None,
        "usage": None, "phase_tokens": {}, "cache_read_share": None, "cost_usd": None,
    }
    u = _usage(project, st) if with_usage else None
    if isinstance(u, dict):
        card["usage"] = {k: int(u.get(k) or 0) for k in ("input", "output", "cache_write", "cache_read", "total")}
        card["phase_tokens"] = {str(k): int(v) for k, v in (u.get("phases") or {}).items() if isinstance(v, int)}
        side = card["usage"]["input"] + card["usage"]["cache_write"] + card["usage"]["cache_read"]
        card["cache_read_share"] = (card["usage"]["cache_read"] / side) if side else None
        card["cost_usd"] = cost_usd(card["usage"])
    return card


def _pct(x: Optional[float]) -> str:
    return "?" if x is None else f"{x * 100:.0f}%"


def lines(card: Dict[str, Any], t: Callable[..., str], mark: str = "") -> List[str]:
    """The scorecard as at most four human lines; `t` is a catalog lookup (`i18n.t` or one language's table).
    The token line is left out when no usage is known."""
    sfx = (" " + mark) if mark else ""
    out = [t("evidence.sc_quality", rate=_pct(card["first_pass_rate"]), first=card["first_pass"], done=card["done"],
             reopens=card["reopens"], per=("?" if card["reopens_per_task"] is None else f"{card['reopens_per_task']:.2f}"),
             regressions=card["regressions_caught"]) + sfx]
    first_red = T.compact_dur(card["time_to_first_red"]) if card["time_to_first_red"] is not None else "?"
    mix = ", ".join(f"{k} {v}" for k, v in sorted(card["red_reasons"].items()))
    out.append(t("evidence.sc_flow", first_red=first_red, deferred=card["deferred"], blocked=card["blocked"])
               + (t("evidence.sc_red_mix", mix=mix) if mix else "") + sfx)
    u = card["usage"]
    if u:
        phases = ", ".join(f"{k} {v}" for k, v in card["phase_tokens"].items())
        out.append(t("evidence.sc_tokens", total=u["total"], input=u["input"], output=u["output"], cache=_pct(card["cache_read_share"]))
                   + (t("evidence.sc_phases", phases=phases) if phases else "") + sfx)
        cost = card["cost_usd"]
        out.append(t("evidence.sc_cost", cost=f"{cost:.2f}") + sfx if cost is not None
                   else t("evidence.sc_cost_none") + sfx)
    return out


def warning(card: Dict[str, Any], t: Callable[..., str]) -> Optional[str]:
    """Soft warning when the estimated cost passes `scorecard.cost_warn_usd` (0 or unset: off)."""
    limit, cost = warn_threshold(), card.get("cost_usd")
    if limit > 0 and cost is not None and cost > limit:
        return t("evidence.sc_cost_warn", cost=f"{cost:.2f}", limit=f"{limit:.2f}")
    return None

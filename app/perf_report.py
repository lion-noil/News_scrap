# -*- coding: utf-8 -*-
"""트레이딩봇 성적표 (2026-09-26 도입) — 주별·월별 실현손익·수익률 vs 월 2% 목표. 사이트/앱 열람용(perf:latest).

원천(사이트 API와 동일): Supabase trade_records(EXIT pnl: BYBIT=USDT, MT5=USD·09-18 정정치),
  Bybit 일별 에쿼티 = Supabase asset_snapshots(equity_usdt), MT5 일별 에쿼티 = Upstash 해시(persist.MT5_DAILY_EQUITY_KEY).
수익률 = 기간 실현 ÷ 기간 시작 에쿼티(직전 관측값). 에쿼티Δ = 기간 말/초 변화(미실현·입출금 포함).
usage: python perf_report.py [--publish] [--json]   (매일 07:40 main.py 가 --publish 로 실행 → report_store perf:latest)
"""
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from monthly_report import KST, SEGMENT_KO, fetch_rows, segment_of, segment_order
from persist import MT5_DAILY_EQUITY_KEY, get_supabase, news_redis

log = logging.getLogger("perf_report")
TARGET_M = 2.0                      # 월 목표 %
TARGET_W = TARGET_M / (52 / 12)     # 주 환산 ≈ 0.46%
ACCOUNTS = [("BYBIT", SEGMENT_KO["BYBIT"], "USDT"), ("MT5", SEGMENT_KO["MT5"], "USD")]
WEEKS_KEEP = 26


def _bybit_equity(sb) -> dict:
    out = {}
    rows = (sb.table("asset_snapshots").select("day, created_at, equity_usdt, wallet_usdt, raw_json")
            .order("day", desc=True).order("created_at", desc=True).limit(2000).execute().data or [])
    for r in rows:
        d = r.get("day")
        if not d or d in out:
            continue
        raw = r.get("raw_json") or {}
        for v in (r.get("equity_usdt"), raw.get("equity_usdt") if isinstance(raw, dict) else None,
                  r.get("wallet_usdt"), raw.get("wallet.USDT") if isinstance(raw, dict) else None):
            try:
                if v is not None:
                    out[d] = float(v)
                    break
            except (TypeError, ValueError):
                continue
    return out


def _mt5_equity() -> dict:
    out = {}
    for k, v in (news_redis.hgetall(MT5_DAILY_EQUITY_KEY) or {}).items():
        d = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        try:
            j = json.loads(v.decode() if isinstance(v, (bytes, bytearray)) else v)
            out[d] = float(j.get("equity_usd") or j.get("wallet_usd"))
        except Exception:  # noqa: BLE001
            continue
    return out


def _eq_at(series: dict, day: str):
    keys = sorted(series)
    prev = [k for k in keys if k <= day]
    if prev:
        return series[prev[-1]]
    return series[keys[0]] if keys else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build() -> tuple[dict, str]:
    now = datetime.now(KST)
    today = now.date()
    sb = get_supabase()
    rows = fetch_rows(sb, "2026-01-01", (today + timedelta(days=1)).isoformat())
    eq = {"BYBIT": _bybit_equity(sb), "MT5": _mt5_equity()}

    exits = []
    for r in rows:
        raw = r.get("raw_json") or {}
        if not isinstance(raw, dict) or str(r.get("kind") or "").upper() != "EXIT":
            continue
        try:
            pnl = float(r.get("pnl"))
        except (TypeError, ValueError):
            continue
        ts = int(raw.get("ts_ms") or 0)
        d = datetime.fromtimestamp(ts / 1000, KST).date() if ts else date.fromisoformat(str(r.get("day")))
        acc = str(raw.get("account") or "BYBIT")
        exits.append((d, acc, segment_of(acc, r.get("symbol")), pnl))

    # 블록 = 계좌 전체 + (MT5만) 자산군 구분. 구분의 분모는 그 계좌 전체 에쿼티 → 구분 수익률 합 = 계좌 수익률.
    blocks = []
    for acc, name, ccy in ACCOUNTS:
        blocks.append((acc, name, ccy, acc, None))
        for seg in sorted({g for _, a, g, _ in exits if a == acc and g != acc}, key=segment_order):
            blocks.append((seg, SEGMENT_KO.get(seg, seg), ccy, acc, acc))

    accounts = []
    this_ym = today.strftime("%Y-%m")
    this_mon = _monday(today)
    for key, name, ccy, acc, parent in blocks:
        mine = [(d, p) for d, a, g, p in exits if (g if parent else a) == key]
        s = eq[acc]
        wk = defaultdict(lambda: [0, 0.0])
        mo = defaultdict(lambda: [0, 0.0])
        for d, p in mine:
            wk[_monday(d)][0] += 1
            wk[_monday(d)][1] += p
            mo[d.strftime("%Y-%m")][0] += 1
            mo[d.strftime("%Y-%m")][1] += p
        weeks = []
        for m in sorted(wk)[-WEEKS_KEEP:]:
            n, p = wk[m]
            e0 = _eq_at(s, m.isoformat())
            e1 = _eq_at(s, (m + timedelta(days=6)).isoformat())
            pct = (p / e0 * 100) if e0 else None
            eqd = ((e1 / e0 - 1) * 100) if (not parent and e0 and e1 and s and min(s) <= m.isoformat()) else None
            weeks.append({"label": f"{m.isocalendar()[0]}-W{m.isocalendar()[1]:02d}", "monday": m.isoformat(), "n": n,
                          "realized": round(p, 2), "eq0": None if e0 is None else round(e0, 2),
                          "pct": None if pct is None else round(pct, 2), "eqd": None if eqd is None else round(eqd, 2),
                          "hit": None if pct is None else pct >= TARGET_W, "partial": m == this_mon})
        months = []
        for ym in sorted(mo):
            n, p = mo[ym]
            e0 = _eq_at(s, f"{ym}-01")
            pct = (p / e0 * 100) if e0 else None
            months.append({"ym": ym, "n": n, "realized": round(p, 2), "eq0": None if e0 is None else round(e0, 2),
                           "pct": None if pct is None else round(pct, 2),
                           "hit": None if pct is None else pct >= TARGET_M, "partial": ym == this_ym})
        full = [x for x in months if not x["partial"] and x["pct"] is not None and x["n"]]
        recent = weeks[-8:]
        rec_real = sum(w["realized"] for w in recent)
        rec_e0 = recent[0]["eq0"] if recent and recent[0]["eq0"] else None
        first_day, last_day = (min(s), max(s)) if s else (None, None)
        own_eq = bool(s) and not parent     # 에쿼티 줄은 계좌 전체 블록에만(구분은 계좌 에쿼티를 공유)
        accounts.append({
            "account": key, "name": name, "currency": ccy, "parent": parent,
            "realized_total": round(sum(p for _, p in mine), 2), "exits": len(mine),
            "equity_first": {"day": first_day, "value": round(s[first_day], 2)} if own_eq else None,
            "equity_last": {"day": last_day, "value": round(s[last_day], 2)} if own_eq else None,
            "equity_change_pct": round((s[last_day] / s[first_day] - 1) * 100, 2) if own_eq and s[first_day] else None,
            "recent8w": {"realized": round(rec_real, 2), "pct": None if not rec_e0 else round(rec_real / rec_e0 * 100, 2),
                         "weeks": len(recent), "hit_weeks": sum(1 for w in recent if w["hit"])},
            "months_avg_pct": None if not full else round(sum(x["pct"] for x in full) / len(full), 2),
            "months_hit": f"{sum(1 for x in full if x['hit'])}/{len(full)}" if full else "",
            "weeks": weeks, "months": months,
        })

    data = {"kind": "perf", "generated_at": now.isoformat(timespec="seconds"), "target_month_pct": TARGET_M,
            "target_week_pct": round(TARGET_W, 2), "accounts": accounts,
            "note": "실현=청산 손익 합(수수료 반영, MT5는 09-18 정정치). 수익률=실현÷기간 시작 에쿼티. "
                    "에쿼티Δ에는 미실현·입출금이 섞임. 미청산 포지션은 실현에 없음. "
                    "지수·환율·원자재·코인CFD(MT5)는 한 MT5 계좌를 자산군으로 나눈 것 — 분모는 MT5 계좌 전체 에쿼티라 "
                    "구분별 수익률을 합치면 'CFD 계좌 전체' 수익률이 된다."}

    # md (텍스트 폴백)
    L = [f"# 트레이딩봇 성적표 — {now:%Y-%m-%d} (목표 월 {TARGET_M:.0f}%)\n", data["note"] + "\n"]
    for a in accounts:
        L.append(f"## {a['name']}\n")
        if a["equity_first"]:
            L.append(f"- 에쿼티 {a['equity_first']['value']:,.0f} ({a['equity_first']['day']}) → "
                     f"{a['equity_last']['value']:,.0f} ({a['equity_last']['day']}) = {a['equity_change_pct']:+.1f}% · "
                     f"실현 합계 {a['realized_total']:+.2f} {a['currency']} ({a['exits']}건)")
        else:
            L.append(f"- 실현 합계 {a['realized_total']:+.2f} {a['currency']} ({a['exits']}건)")
        r8 = a["recent8w"]
        L.append(f"- 최근 {r8['weeks']}주: 실현 {r8['realized']:+.2f} {a['currency']}"
                 + (f" ({r8['pct']:+.1f}%)" if r8["pct"] is not None else "") + f" · 주 목표({TARGET_W:.2f}%) 달성 {r8['hit_weeks']}/{r8['weeks']}주"
                 + (f" · 완결 월 평균 {a['months_avg_pct']:+.2f}%/월 (달성 {a['months_hit']})" if a["months_avg_pct"] is not None else ""))
        L.append("\n| 월 | n | 실현 | 월초에쿼티 | 수익률 | 목표 |\n|---|---|---|---|---|---|")
        for m in a["months"]:
            flag = "" if m["hit"] is None else ("✅" if m["hit"] else "✗") + (" 진행중" if m["partial"] else "")
            e0 = "" if m["eq0"] is None else "{:,.0f}".format(m["eq0"])
            pc = "" if m["pct"] is None else "{:+.2f}%".format(m["pct"])
            L.append(f"| {m['ym']} | {m['n']} | {m['realized']:+.2f} | {e0} | {pc} | {flag} |")
        L.append("\n| 주 | n | 실현 | 시작에쿼티 | 수익률 | 에쿼티Δ | 목표 |\n|---|---|---|---|---|---|---|")
        for w in a["weeks"][-12:]:
            flag = "" if w["hit"] is None else ("✅" if w["hit"] else "✗")
            e0 = "" if w["eq0"] is None else "{:,.0f}".format(w["eq0"])
            pc = "" if w["pct"] is None else "{:+.2f}%".format(w["pct"])
            ed = "" if w["eqd"] is None else "{:+.2f}%".format(w["eqd"])
            L.append(f"| {w['label']} | {w['n']} | {w['realized']:+.2f} | {e0} | {pc} | {ed} | {flag} |")
        L.append("")
    return data, "\n".join(L)


def subtitle_of(data: dict) -> str:
    parts = []
    for a in data["accounts"]:
        if a.get("parent"):
            continue
        cur = next((m for m in a["months"] if m["partial"]), None)
        if cur and cur["pct"] is not None:
            parts.append(f"{a['name'].split('(')[0]} 이달 {cur['pct']:+.1f}%")
    return " · ".join(parts) or "성적표"


def main():
    data, md = build()
    if "--publish" in sys.argv:
        from report_store import publish_report
        publish_report("perf", "latest", md, title=f"📈 성적표 — 주별·월별 수익률 (목표 월 {TARGET_M:.0f}%)",
                       subtitle=subtitle_of(data), data=data)
        print("published perf:latest", file=sys.stderr)
    print(json.dumps(data, ensure_ascii=False, default=str) if "--json" in sys.argv else md)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    main()

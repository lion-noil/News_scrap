# -*- coding: utf-8 -*-
"""트레이딩봇 성적표 (2026-09-26 도입) — 주별·월별 실현손익·수익률 vs 월 2% 목표. 사이트/앱 열람용(perf:latest).

원천(사이트 API와 동일): Supabase trade_records(EXIT pnl: BYBIT=USDT, MT5=USD·09-18 정정치),
  Bybit 일별 에쿼티 = Supabase asset_snapshots(equity_usdt), MT5 일별 에쿼티 = Upstash 해시(persist.MT5_DAILY_EQUITY_KEY).
수익률 = 기간 실현 ÷ 기간 시작 에쿼티(직전 관측값). 에쿼티Δ = 기간 말/초 변화(미실현·입출금 포함).
지표(stats, 10-04): 진입 빈도·건당 평균·승률·낙폭을 시뮬 기준(perf_benchmark.json)과 나란히. 집계 구간은 STATS_FROM(현행 셀 확정일)
  이후 — 시뮬이 같은 셀 구성이라 그 전(구 전략)과는 비교가 안 된다. 낙폭 = 일별 실현 수익률 누적 곡선의 고점 대비 하락(%p, 시뮬과 같은 정의).
  기준 갱신: reversion_research 에서 `py -3 backtests/perf_benchmark.py` (셀·사이징 바뀌면 다시 돌릴 것).
usage: python perf_report.py [--publish] [--json]   (매일 07:40 main.py 가 --publish 로 실행 → report_store perf:latest)
"""
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import equity_hist
from monthly_report import KST, SEGMENT_KO, fetch_rows, segment_of, segment_order
from persist import MT5_DAILY_EQUITY_KEY, MT5FX_DAILY_EQUITY_KEY, get_supabase, news_redis

log = logging.getLogger("perf_report")
TARGET_M = 2.0                      # 월 목표 %
TARGET_W = TARGET_M / (52 / 12)     # 주 환산 ≈ 0.46%
ACCOUNTS = [("BYBIT", SEGMENT_KO["BYBIT"], "USDT"), ("MT5", SEGMENT_KO["MT5"], "USD")]
WEEKS_KEEP = 26
PROP_TOTAL_DD = 10.0    # 프랍 2-step 통상 규칙(평가손 포함): 총 낙폭 10%
PROP_DAILY_DD = 5.0     #   하루 낙폭 5% (1-step 은 3%)
STATS_FROM = date(2026, 7, 16)      # 현행 라이브 셀 확정일(live_cells.json) — 시뮬 기준과 같은 구성인 구간


def _benchmark() -> dict:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "perf_benchmark.json"), encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as e:  # noqa: BLE001 — 기준 파일이 없어도 성적표는 나간다
        log.warning("perf_benchmark.json 읽기 실패: %s", e)
        return {}


def _drawdown(daily: dict) -> dict:
    """일별 실현 수익률(%p) 누적 곡선의 최대 낙폭·현재 낙폭. daily = {date: 그날 수익률}"""
    cum = peak = mdd = 0.0
    days = sorted(daily)
    peak_day = days[0] if days else None      # 첫날부터 손실이면 시작일이 고점
    mdd_from = mdd_to = None
    for d in days:
        cum += daily[d]
        if cum >= peak:
            peak, peak_day = cum, d
        elif peak - cum > mdd:
            mdd, mdd_from, mdd_to = peak - cum, peak_day, d
    return {"mdd": round(mdd, 2), "mdd_from": mdd_from and mdd_from.isoformat(), "mdd_to": mdd_to and mdd_to.isoformat(),
            "dd_now": round(peak - cum, 2), "peak_day": peak_day and peak_day.isoformat()}


def _stats(mine: list, entry_days: list, s: dict, so: dict, own_eq: bool, sim: dict, today: date) -> dict:
    """STATS_FROM 이후 진입 빈도·건당 평균·승률·낙폭 + 시뮬 기준. mine=[(청산일, pnl)], entry_days=[진입일]"""
    ex = [(d, p) for d, p in mine if d >= STATS_FROM]
    ent = [d for d in entry_days if d >= STATS_FROM]
    months = max((today - STATS_FROM).days, 1) / 30.44
    daily, contrib = defaultdict(float), []
    for d, p in ex:
        e0 = _eq_at(s, d.isoformat())
        if e0:
            c = p / e0 * 100
            daily[d] += c
            contrib.append(c)
    out = {"from": STATS_FROM.isoformat(), "entries": len(ent), "exits": len(ex),
           "entries_per_month": round(len(ent) / months, 1),
           "trade_avg_eq_pct": round(sum(contrib) / len(contrib), 3) if contrib else None,
           "win_rate": round(100 * sum(1 for _, p in ex if p > 0) / len(ex), 1) if ex else None}
    out.update(_drawdown(daily))
    if own_eq:      # 에쿼티 고점 대비(미실현 포함, 입출금도 섞임) — 계좌 블록만
        days = [k for k in sorted(so) if k >= STATS_FROM.isoformat()]
        run_peak, worst = 0.0, 0.0
        for k in days:
            run_peak = max(run_peak, so[k])
            worst = max(worst, (1 - so[k] / run_peak) * 100 if run_peak else 0.0)
        if days and run_peak:
            out["eq_mdd"] = round(worst, 2)
            out["eq_dd_now"] = round((1 - so[days[-1]] / run_peak) * 100, 2)
    if sim:
        out["sim"] = sim
        out["dd_verdict"] = ("평소 범위" if out["mdd"] <= sim["dd_p95"] else
                             "시뮬 최악 이내" if out["mdd"] <= sim["mdd"] else "시뮬 최악 초과")
    return out


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


def _mt5_equity(key: str = MT5_DAILY_EQUITY_KEY) -> dict:
    out = {}
    for k, v in (news_redis.hgetall(key) or {}).items():
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
    # 환율 전용 MT5 계좌(2026-10-05~): 그날부터 환율 구분의 분모·에쿼티는 그 계좌 값. 그 전은 한 계좌였으므로 MT5 값.
    fx_eq = _mt5_equity(MT5FX_DAILY_EQUITY_KEY)
    own_series = {}
    if fx_eq:
        fx0 = min(fx_eq)
        eq["MT5-환율"] = {**{d: v for d, v in eq["MT5"].items() if d < fx0}, **fx_eq}
        own_series = {"MT5-환율": fx_eq, "MT5-비환율": eq["MT5"]}

    bench = _benchmark()
    equity_hist.rollup_all(3)       # 평가액 이력 → 일별 요약 최신화(멱등)
    exits, entries = [], []
    for r in rows:
        raw = r.get("raw_json") or {}
        kind = str(r.get("kind") or "").upper()
        if not isinstance(raw, dict) or kind not in ("EXIT", "ENTRY"):
            continue
        ts = int(raw.get("ts_ms") or 0)
        d = datetime.fromtimestamp(ts / 1000, KST).date() if ts else date.fromisoformat(str(r.get("day")))
        acc = str(raw.get("account") or "BYBIT")
        if kind == "ENTRY":
            entries.append((d, acc, segment_of(acc, r.get("symbol"))))
            continue
        try:
            pnl = float(r.get("pnl"))
        except (TypeError, ValueError):
            continue
        exits.append((d, acc, segment_of(acc, r.get("symbol")), pnl))

    # 블록 = (MT5만) 구분별 → 계좌 합계. 구분의 분모는 그 계좌 전체 에쿼티 → 구분 수익률 합 = 계좌 수익률.
    blocks = []
    for acc, name, ccy in ACCOUNTS:
        for seg in sorted({g for _, a, g, _ in exits if a == acc and g != acc}, key=segment_order):
            blocks.append((seg, SEGMENT_KO.get(seg, seg), ccy, acc, acc))
        blocks.append((acc, name, ccy, acc, None))

    accounts = []
    this_ym = today.strftime("%Y-%m")
    this_mon = _monday(today)
    for key, name, ccy, acc, parent in blocks:
        mine = [(d, p) for d, a, g, p in exits if (g if parent else a) == key]
        entry_days = [d for d, a, g in entries if (g if parent else a) == key]
        ent_mo = defaultdict(int)
        for d in entry_days:
            ent_mo[d.strftime("%Y-%m")] += 1
        s = eq.get(key) or eq[acc]
        so = own_series.get(key) or s      # 에쿼티 줄·에쿼티 낙폭에 쓰는 '그 계좌만의' 시리즈
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
        sim = (bench.get("segments") or {}).get(key) or {}
        sim_dd, sim_en = sim.get("month_dd"), sim.get("month_entries")
        for ym in sorted(mo):
            n, p = mo[ym]
            e0 = _eq_at(s, f"{ym}-01")
            pct = (p / e0 * 100) if e0 else None
            partial = ym == this_ym
            # 달 안 낙폭: 월초 0 에서 시작한 일별 실현 수익률(월초 에쿼티 대비) 누적의 고점 대비 최대 하락 — 시뮬 month_dd 와 같은 정의
            day_pct = defaultdict(float)
            for d, x in mine:
                if e0 and d.strftime("%Y-%m") == ym:
                    day_pct[d] += x / e0 * 100
            cum = peak = dd = 0.0
            for d in sorted(day_pct):
                cum += day_pct[d]
                peak = max(peak, cum)
                dd = max(dd, peak - cum)
            ent = ent_mo.get(ym, 0)
            # 시뮬 적합 판정은 현행 셀로만 돈 달(확정 다음 달~)에만. 진행 중인 달의 진입은 '많음'만 판정(적음은 아직 모름).
            judge = ym > STATS_FROM.strftime("%Y-%m")
            dd_fit = en_fit = None
            if judge and sim_dd and e0:
                dd_fit = "정상" if dd <= sim_dd["p95"] else ("주의" if dd <= sim_dd["max"] else "이탈")
            if judge and sim_en:
                en_fit = "많음" if ent > sim_en["p95"] else (None if partial else ("적음" if ent < sim_en["p5"] else "적합"))
            months.append({"ym": ym, "n": n, "entries": ent, "entries_fit": en_fit, "realized": round(p, 2),
                           "eq0": None if e0 is None else round(e0, 2),
                           "pct": None if pct is None else round(pct, 2),
                           "dd": round(dd, 2) if e0 else None, "dd_fit": dd_fit,
                           "hit": None if pct is None else pct >= TARGET_M, "partial": partial})
        # 평가손 포함 낙폭(월중 평가액 고점 대비)·하루 최대 낙폭 — 실행기 평가액 이력(2026-10-05~)이 있는 달만
        mtm = equity_hist.month_stats(equity_hist.load_days(equity_hist.ACCOUNTS[key])) if key in equity_hist.ACCOUNTS else {}
        have = {x["ym"] for x in months}
        for ym in sorted(set(mtm) - have):      # 청산이 없어 표에 없던 달도 평가액 기록이 있으면 줄을 만든다
            e0 = _eq_at(s, f"{ym}-01")
            months.append({"ym": ym, "n": 0, "entries": ent_mo.get(ym, 0), "entries_fit": None, "realized": 0.0,
                           "eq0": None if e0 is None else round(e0, 2), "pct": 0.0 if e0 else None, "dd": 0.0 if e0 else None,
                           "dd_fit": None, "hit": False if e0 else None, "partial": ym == this_ym})
        months.sort(key=lambda x: x["ym"])
        for x in months:
            ms = mtm.get(x["ym"])
            x["mtm_dd"] = ms and ms["mtm_dd"]
            x["day_dd_max"] = ms and ms["day_dd_max"]
            x["mtm_days"] = ms and ms["days"]
        full = [x for x in months if not x["partial"] and x["pct"] is not None and x["n"]]
        recent = weeks[-8:]
        rec_real = sum(w["realized"] for w in recent)
        rec_e0 = recent[0]["eq0"] if recent and recent[0]["eq0"] else None
        own_eq = bool(so) and (not parent or key in own_series)   # 에쿼티 줄: 계좌 블록 + 계좌가 따로 있는 구분
        first_day, last_day = (min(so), max(so)) if so else (None, None)
        accounts.append({
            "account": key, "name": name, "currency": ccy, "parent": parent,
            "realized_total": round(sum(p for _, p in mine), 2), "exits": len(mine),
            "equity_first": {"day": first_day, "value": round(so[first_day], 2)} if own_eq else None,
            "equity_last": {"day": last_day, "value": round(so[last_day], 2)} if own_eq else None,
            "equity_change_pct": round((so[last_day] / so[first_day] - 1) * 100, 2) if own_eq and so[first_day] else None,
            "recent8w": {"realized": round(rec_real, 2), "pct": None if not rec_e0 else round(rec_real / rec_e0 * 100, 2),
                         "weeks": len(recent), "hit_weeks": sum(1 for w in recent if w["hit"])},
            "months_avg_pct": None if not full else round(sum(x["pct"] for x in full) / len(full), 2),
            "months_hit": f"{sum(1 for x in full if x['hit'])}/{len(full)}" if full else "",
            "stats": _stats(mine, entry_days, s, so, own_eq, (bench.get("segments") or {}).get(key), today),
            "weeks": weeks, "months": months,
        })

    # 구분으로 나뉜 계좌(MT5)의 합계 블록은 싣지 않는다(10-05 사용자 지시 — 비환율·환율을 따로 본다).
    # 에쿼티는 아직 한 계좌 값 하나뿐이라 참고 문장으로만 남긴다.
    split = {a["parent"] for a in accounts if a.get("parent")}
    shared = " ".join(
        f"{a['account']} 에쿼티(비환율·환율 공유) {a['equity_first']['value']:,.0f} → {a['equity_last']['value']:,.0f} {a['currency']}"
        f"({a['equity_change_pct']:+.1f}%), 에쿼티 낙폭 최대 {a['stats'].get('eq_mdd', 0):.1f}%·지금 {a['stats'].get('eq_dd_now', 0):.1f}%."
        for a in accounts if a["account"] in split and a["equity_last"])
    accounts = [a for a in accounts if a["account"] not in split]

    data = {"kind": "perf", "generated_at": now.isoformat(timespec="seconds"), "target_month_pct": TARGET_M,
            "target_week_pct": round(TARGET_W, 2), "accounts": accounts,
            "prop": {"total_dd": PROP_TOTAL_DD, "daily_dd": PROP_DAILY_DD, "since": "2026-10-05"},
            "note": "실현=청산 손익 합(수수료 반영, MT5는 09-18 정정치). 수익률=실현÷기간 시작 에쿼티. "
                    "에쿼티Δ에는 미실현·입출금이 섞임. 미청산 포지션은 실현에 없음. "
                    + (f"환율은 {min(fx_eq)[5:]}부터 전용 MT5 계좌(FXmode)에서 주문 — 그날부터 환율의 분모·에쿼티는 그 계좌 값, "
                       "그 전 달은 한 계좌였던 MT5 에쿼티가 분모다. 비환율 에쿼티에는 분리 전 환율 손익이 섞여 있다. " if fx_eq else
                       "비환율(지수·금속·원유·코인CFD)·환율의 수익률 분모는 MT5 에쿼티 전체(한 계좌). " + shared + " ") +
                    f"진입 빈도·건당 평균·승률·낙폭은 {STATS_FROM:%m-%d}(현행 셀 확정) 이후 집계. 낙폭=일별 실현 수익률 누적의 고점 대비 "
                    "하락(%p)이라 미실현 손실은 빠져 있다(에쿼티 낙폭은 미실현 포함이지만 입출금도 섞임). "
                    + (f"시뮬 기준={bench.get('window')} 같은 셀·사이징 재현(봉마감 체결·단순합): 평소 범위=시뮬 날짜의 95%가 "
                       "이보다 얕았던 낙폭, 최악=5년 최대 낙폭. 월별 표의 낙폭=그 달 안에서(월초 0 기준) 고점 대비 최대 하락, "
                       "판정은 시뮬 60개 달 분포 대비 — 낙폭: 정상(95% 이내)·주의(시뮬 최악 달 이내)·이탈(최악 초과), "
                       "진입: 적합(5~95% 범위)·많음·적음. 판정은 현행 셀로만 돈 08월부터, 진행 중인 달은 진입 '많음'만 본다. " if bench else "")
                    + f"평가낙폭=들고 있는 포지션의 평가손까지 넣은 계좌 평가액의 월중 고점 대비 최대 하락, 일최대=하루({equity_hist.DAY_START_HOUR}시 "
                      "시작) 시작 평가액 대비 최저. 실행기가 평가액을 MT5 1분·Bybit 5분마다 기록한 2026-10-05부터만 있고(그 사이 순간 저점은 "
                      f"놓칠 수 있음), 입출금은 뺀다. 프랍 통상 규칙은 총 {PROP_TOTAL_DD:.0f}%·하루 {PROP_DAILY_DD:.0f}%.",
            "benchmark": {k: bench.get(k) for k in ("generated_at", "window", "basis")} if bench else None}

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
        st = a["stats"]
        sim = st.get("sim") or {}
        L.append(f"- {st['from'][5:]} 이후: 진입 {st['entries']}건(월 {st['entries_per_month']}건"
                 + (f" · 시뮬 {sim['trades_per_month']}건" if sim else "") + ")"
                 + ("" if st["trade_avg_eq_pct"] is None else
                    f" · 건당 {st['trade_avg_eq_pct']:+.3f}%" + (f"(시뮬 {sim['trade_avg_eq_pct']:+.3f}%)" if sim else ""))
                 + ("" if st["win_rate"] is None else
                    f" · 승률 {st['win_rate']:.0f}%" + (f"(시뮬 {sim['win_rate']:.0f}%)" if sim else "")))
        L.append(f"- 낙폭(실현 기준): 최대 {st['mdd']:.2f}%p"
                 + (f" ({st['mdd_from'][5:]}~{st['mdd_to'][5:]})" if st["mdd_to"] else "") + f" · 지금 {st['dd_now']:.2f}%p"
                 + (f" · 시뮬 평소 {sim['dd_p95']:.2f}/최악 {sim['mdd']:.2f}%p → {st['dd_verdict']}" if sim else "")
                 + (f" · 에쿼티 낙폭 최대 {st['eq_mdd']:.1f}%/지금 {st['eq_dd_now']:.1f}%" if st.get("eq_mdd") is not None else ""))
        if sim.get("month_dd"):
            sd, se = sim["month_dd"], sim["month_entries"]
            L.append(f"- 달별 시뮬 기준: 진입 {se['p5']:.0f}~{se['p95']:.0f}건(중앙 {se['p50']:.0f}) · "
                     f"달 안 낙폭 평소 {sd['p50']:.2f}%p, 95% {sd['p95']:.2f}%p, 최악 {sd['max']:.2f}%p")
        L.append("\n| 월 | 진입 | 시뮬 적합 | 청산 | 실현 | 월초에쿼티 | 수익률 | 목표 | 낙폭 | 시뮬 적합 | 평가낙폭 | 일최대 |\n|---|---|---|---|---|---|---|---|---|---|---|---|")
        for m in a["months"]:
            flag = "" if m["hit"] is None else ("✅" if m["hit"] else "✗") + (" 진행중" if m["partial"] else "")
            e0 = "" if m["eq0"] is None else "{:,.0f}".format(m["eq0"])
            pc = "" if m["pct"] is None else "{:+.2f}%".format(m["pct"])
            ddv = "" if m["dd"] is None else "{:.2f}%p".format(m["dd"])
            L.append(f"| {m['ym']} | {m['entries']} | {m['entries_fit'] or ''} | {m['n']} | {m['realized']:+.2f} | {e0} | {pc} | {flag} | "
                     f"{ddv} | {m['dd_fit'] or ''} | "
                     + ("" if m.get("mtm_dd") is None else f"{m['mtm_dd']:.2f}%") + " | "
                     + ("" if m.get("day_dd_max") is None else f"{m['day_dd_max']:.2f}%") + " |")
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
    split = {a["parent"] for a in data["accounts"] if a.get("parent")}
    for a in data["accounts"]:
        if a["account"] in split:      # 구분으로 나뉜 계좌의 합계 블록은 부제에서 생략
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

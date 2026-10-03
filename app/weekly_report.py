# -*- coding: utf-8 -*-
"""트레이딩봇 주간 보고서 (2026-09-19 도입, 09-20 읽기 구조 개편) — ISO 주(월~일, KST) 셀별 실측 + 구독 Opus 셀별 평가.

월간 보고서(monthly_report.py)와 같은 원천(Supabase trade_records)·같은 셀 정의(계좌×심볼×책×전략)를 쓰되,
주간의 역할은 '판정'이 아니라 조기 경보다 — 1개월 표본도 노이즈라는 합의 원칙에 따라 파라미터 변경은
월간(🔴 2개월 연속 / 백테스트 최악연도 초과)에서만 다룬다.

읽기 순서(09-20): ① 한눈에(계좌 요약·판정 수·4주 추이) ② 지금 볼 것(점검·주의만) ③ 총평 ④ 관찰·정상 셀 한 줄씩
                 ⑤ 월 누적 🔴/🟡 후보 ⑥ 숫자표(맨 뒤, 6열).  평가는 렌더 전에 먼저 돌고 표가 아니라 리스트로 나간다.
JSON(data)도 함께 만들어 --publish 시 report_store 에 md 와 같이 저장 → 사이트/앱이 판정별 카드·필터를 네이티브로 그린다.

usage: python weekly_report.py [2026-W37 | 2026-09-10] [--no-llm] [--publish] [--json]
       (인자 없음 = 직전 완료 주. 진행 중인 주를 지정하면 '진행중' 표기. --json = md 대신 data JSON 출력)
발행: main.py scheduled_weekly_report(월 07:30) → `--publish` 로 Redis(weekly:{label}) + stdout md 를 텔레그램으로.
"""
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from monthly_report import EXPECTED, KST, SEGMENT_KO, book_of, fetch_rows, segment_of, segment_order
from persist import get_supabase

log = logging.getLogger("weekly_report")

FEE_PCT = 0.11          # 수수료 왕복 근사 (월간과 동일)
TREND_WEEKS = 4         # 추이 표시 주 수
LOOKBACK_WEEKS = 12     # 무진입 연속 판정 창
STALE_MAX_WEEKS = 8     # 이 안에 진입 이력이 있어야 '무진입' 경보 대상

STRAT_KO = {
    "S1": "추세(구채널)", "S2": "역추세(구채널)", "S3": "추세(일봉)", "S4": "역추세(일봉)",
    "S11": "추세추종", "S12": "역추세(급락매수)", "S13": "급락페이드(시간청산 위주)",
    "S14": "ewz추세", "S15": "유동성스윕",
}
ACCOUNT_KO = SEGMENT_KO   # account 자리에는 구분(세그먼트)이 들어간다 — MT5는 비환율·환율로 분리(10-03)
GRADE_ORDER = ["점검", "주의", "관찰", "정상"]
GRADE_ICON = {"점검": "🔧", "주의": "🟠", "관찰": "👀", "정상": "✅"}

EVAL_SYSTEM = """너는 자동매매 봇의 주간 성과 검토자다. 입력은 셀(계좌×심볼×책×전략)별 이번 주·직전 3주·월 누적 실측과 백테스트 기대값(있을 때)이다. 한국어로 답한다.

운영 원칙(사용자와 합의됨, 반드시 지킬 것):
- 1개월 표본도 노이즈다. 파라미터 변경은 월간 보고서에서 🔴가 2개월 연속이거나 백테스트 최악연도를 넘길 때만 검토한다. 주간 평가에서는 절대 파라미터 변경·셀 중단을 지시하지 않는다.
- 주간의 역할은 조기 경보다: (a) 국면(regime) 변화 vs 엣지 소멸 vs 운영 결함(피드 끊김·청산 누락)을 구분하고, (b) 월간 판정 후보를 미리 표시한다.
- 무거래 셀은 발동조건 미도달이면 정상이다. 단, 평소 매주 진입하던 셀이 2주 이상 0건이면 피드/구독 결함을 의심한다(2026-09-16 MT5 지수 3종이 08-27부터 틱 미수신으로 신호 0건이던 실사고).
- 미청산이 많은 셀은 결과 미확정이다. 청산%만으로 단정하지 않는다.
- 전략 의미: S11/S3 추세추종, S12/S4 역추세(급락매수), S13 급락페이드(1분, 시간청산 위주라 되돌림 못 잡으면 반납), S14 ewz추세, S15 유동성스윕, S1/S2 구채널(잔여 드레인 중 — 조정 대상 아님). 책: 1분/4h/일봉(만기가 길수록 표본이 적고 미청산이 많다).
- 입력의 account 는 구분이다(BYBIT=크립토, MT5-비환율=지수·금속·원유·코인CFD, MT5-환율=FX — 지금은 한 MT5 계좌지만 별도 계좌처럼 본다). 총평은 그 구분만 놓고 쓴다.
- 같은 계좌의 다른 셀과 같은 주에 같은 방향으로 손실이 몰렸다면 개별 셀 문제가 아니라 국면(급락 클러스터 등)일 가능성을 먼저 본다.
- 입력 필드 뜻: this.entries/exits = 이번 주 진입/청산 건수(청산은 이전 주 진입분 포함). this.open = 이번 주 진입 중 '지금 시점'까지 미청산인 건수 — 이후 주에 청산됐으면 0이다. 그러므로 "진입은 있는데 청산·미청산이 둘 다 0"은 집계 결함이 아니라 다음 주에 청산된 것이니 점검 사유로 삼지 않는다. mtd = 이번 달 1일~주말 누적(청산 기준).

판정 등급(grade):
- 정상: 기대 범위 내이거나 표본 부족(청산 3건 미만)이며 이상 징후 없음
- 관찰: 손실이지만 국면으로 설명되거나 표본이 작음
- 주의: 월누적 🔴 후보(합계 ≤ -3%p, 청산 3건 이상)이거나 4주 연속 손실·승률 급락 등 엣지 소멸이 의심됨
- 점검: 운영 결함 의심(무진입 연속, 진입만 있고 청산이 만기 이상 없음 등)

각 셀마다 grade, reason(한 문장 60자 안팎, 반드시 숫자 인용), action(다음 중 하나로 시작: "없음" / "관찰 유지" / "월간에서 재검토 후보" / "운영 점검: <무엇을>")을 쓴다.
key는 입력의 key를 그대로 돌려준다. 입력에 없는 셀을 만들지 않는다. 마지막 summary는 이 계좌의 총평 2~3문장(이번 주 특징, 가장 신경 쓸 셀 1~2개)."""


def _eval_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cells": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string"},
                        "grade": {"type": "string", "enum": GRADE_ORDER},
                        "reason": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["key", "grade", "reason", "action"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["cells", "summary"],
    }


# ───────────────────────────────────────────────────────────
# 주 범위 · 행 해석
# ───────────────────────────────────────────────────────────
def parse_week(arg: str | None):
    """→ (label '2026-W37', week_start(KST 월 00:00), week_end(배타, 다음 월 00:00))"""
    now = datetime.now(KST)
    if not arg:
        d = (now - timedelta(days=now.weekday() + 7)).date()      # 직전 완료 주의 월요일
    elif "W" in arg.upper():
        y, w = arg.upper().split("-W")
        d = date.fromisocalendar(int(y), int(w), 1)
    else:
        d0 = date.fromisoformat(arg)
        d = d0 - timedelta(days=d0.weekday())
    start = datetime(d.year, d.month, d.day, tzinfo=KST)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", start, start + timedelta(days=7)


def _monday(dt: datetime) -> date:
    d = dt.date()
    return d - timedelta(days=d.weekday())


def _wlabel(monday: date) -> str:
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def _row_dt(raw: dict, r: dict) -> datetime | None:
    try:
        ms = int(raw.get("ts_ms") or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=KST)
    try:  # ts_ms 없는 구행: day 00:00 KST
        return datetime.fromisoformat(str(r.get("day"))).replace(tzinfo=KST)
    except Exception:
        return None


def _cell_key(r: dict, raw: dict):
    account = str(raw.get("account") or "BYBIT")
    sym = str(r.get("symbol") or "?")
    tag = str(raw.get("strategy_tag") or (str(r.get("signal") or "").split("_")[0]) or "?").upper()
    if not tag.startswith("S"):
        tag = "?"
    return (segment_of(account, sym), sym, book_of(raw.get("signal_ns") or ""), tag)


def _exit_pct(r: dict, raw: dict) -> float | None:
    try:
        ep, xp = float(raw.get("entry_price") or 0), float(raw.get("exit_price") or 0)
    except (TypeError, ValueError):
        return None
    if ep <= 0 or xp <= 0:
        return None
    pct = (xp / ep - 1) * 100
    if str(r.get("side") or "").upper() == "SHORT":
        pct = -pct
    return pct - FEE_PCT


def _new_stat():
    return dict(entries=0, exits=0, wins=0, pcts=[], usdt=0.0, open=0)


def _add_exit(st: dict, r: dict, raw: dict):
    st["exits"] += 1
    pct = _exit_pct(r, raw)
    if pct is not None:
        st["pcts"].append(pct)
        if pct > 0:
            st["wins"] += 1
    try:
        st["usdt"] += float(r.get("pnl"))
    except (TypeError, ValueError):
        pass


def _merge(stats: list[dict]) -> dict:
    out = _new_stat()
    for s in stats:
        for k in ("entries", "exits", "wins", "open"):
            out[k] += s[k]
        out["pcts"] += s["pcts"]
        out["usdt"] += s["usdt"]
    return out


def _wr(st: dict):
    return (100.0 * st["wins"] / len(st["pcts"])) if st["pcts"] else None


def _avg(st: dict):
    return (sum(st["pcts"]) / len(st["pcts"])) if st["pcts"] else None


def _brief(st: dict) -> dict:
    """stat → JSON 친화 요약"""
    return {"entries": st["entries"], "exits": st["exits"], "open": st["open"],
            "win_rate": None if _wr(st) is None else round(_wr(st), 1),
            "avg_pct": None if _avg(st) is None else round(_avg(st), 2),
            "sum_pct": round(sum(st["pcts"]), 2), "realized": round(st["usdt"], 2)}


def _money(account: str, v: float, has: bool) -> str:
    if not has:
        return "—"
    return f"{v:+.2f} {'USDT' if account == 'BYBIT' else 'USD'}"


def _sanitize(s) -> str:
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def _stat_line(c: dict) -> str:
    t = c["this"]
    if t["exits"]:
        s = f"{t['exits']}건 {t['win_rate']:.0f}% {t['sum_pct']:+.1f}%p"
    elif t["entries"]:
        s = f"진입 {t['entries']} · 청산 0"
    else:
        s = f"무진입 {c['streak']}주" if c["streak"] else "거래 없음"
    if t["open"]:
        s += f" · 미청산 {t['open']}"
    return s


def _cell_name(c: dict) -> str:
    return f"{c['symbol']} {c['book']} {c['tag']}"


# ───────────────────────────────────────────────────────────
# 1) 수집·집계
# ───────────────────────────────────────────────────────────
def collect(label: str, ws: datetime, we: datetime) -> dict:
    now = datetime.now(KST)
    partial = we > now
    sb = get_supabase()
    fetch_from = (ws - timedelta(weeks=LOOKBACK_WEEKS) - timedelta(days=1)).date().isoformat()
    fetch_to = (max(we, now) + timedelta(days=1)).date().isoformat()   # 미청산 판정용으로 현재까지
    rows = fetch_rows(sb, fetch_from, fetch_to)

    weeks = [(_monday(ws) - timedelta(weeks=i)) for i in range(LOOKBACK_WEEKS)]   # [이번주, 지난주, ...]
    this_wk = weeks[0]
    trend = weeks[:TREND_WEEKS]
    last_day = we - timedelta(days=1)
    month_start = datetime(last_day.year, last_day.month, 1, tzinfo=KST)

    cell_wk = defaultdict(lambda: defaultdict(_new_stat))
    acct_wk = defaultdict(lambda: defaultdict(_new_stat))
    cell_mtd = defaultdict(_new_stat)
    acct_has_money = defaultdict(bool)
    exit_lots: set[str] = set()
    week_entries: list[tuple[tuple, str]] = []

    for r in rows:
        raw = r.get("raw_json") or {}
        if not isinstance(raw, dict):
            raw = {}
        kind = str(r.get("kind") or "").upper()
        lot = str(raw.get("lot_id") or "")
        if kind == "EXIT" and lot:
            exit_lots.add(lot)
        dt = _row_dt(raw, r)
        if dt is None or dt >= we or dt < ws - timedelta(weeks=LOOKBACK_WEEKS):
            continue
        cell = _cell_key(r, raw)
        wk = _monday(dt)
        in_mtd = dt >= month_start
        if kind == "ENTRY":
            cell_wk[cell][wk]["entries"] += 1
            acct_wk[cell[0]][wk]["entries"] += 1
            if in_mtd:
                cell_mtd[cell]["entries"] += 1
            if wk == this_wk:
                week_entries.append((cell, lot))
        elif kind == "EXIT":
            _add_exit(cell_wk[cell][wk], r, raw)
            _add_exit(acct_wk[cell[0]][wk], r, raw)
            if in_mtd:
                _add_exit(cell_mtd[cell], r, raw)
            if r.get("pnl") is not None:
                acct_has_money[cell[0]] = True
    for cell, lot in week_entries:
        if lot and lot not in exit_lots:
            cell_wk[cell][this_wk]["open"] += 1

    cells: list[dict] = []
    for cell, by_wk in cell_wk.items():
        a, sym, book, tag = cell
        recent = _merge([by_wk[w] for w in trend if w in by_wk])
        this = by_wk.get(this_wk) or _new_stat()
        legacy = book == "?" or tag in ("?", "SCALE")
        if legacy and not (this["entries"] or this["exits"]):
            continue
        streak = 0
        for w in weeks:
            if by_wk.get(w, {}).get("entries", 0) > 0:
                break
            streak += 1
        if not any(by_wk[w]["entries"] > 0 for w in by_wk):
            streak = 0
        if not (recent["entries"] or recent["exits"] or (1 <= streak <= STALE_MAX_WEEKS)):
            continue
        m = cell_mtd.get(cell) or _new_stat()
        mb = _brief(m)
        exp = EXPECTED.get((sym, book, tag))
        flag = None
        if m["exits"] >= 3 and mb["avg_pct"] is not None:
            if mb["sum_pct"] < -3:
                flag = "🔴 월손실"
            elif mb["win_rate"] is not None and mb["win_rate"] < 40:
                flag = "🟡 저승률"
            elif exp and mb["avg_pct"] < exp * 0.3:
                flag = "🟡 기대미달"
        cells.append({
            "key": "|".join(cell), "account": a, "symbol": sym, "book": book, "tag": tag,
            "strategy": STRAT_KO.get(tag, tag),
            "this": _brief(this),
            "trend_sum_pct": [round(sum(by_wk[w]["pcts"]), 2) if w in by_wk else 0.0 for w in trend],
            "trend_exits": [by_wk[w]["exits"] if w in by_wk else 0 for w in trend],
            "recent": _brief(recent), "mtd": mb, "expected_pct": exp,
            "streak": streak, "mtd_flag": flag,
            "grade": None, "reason": "", "action": "",
        })

    accounts = []
    for a in sorted(acct_wk, key=segment_order):
        this = acct_wk[a].get(this_wk) or _new_stat()
        accounts.append({
            "account": a, "name": ACCOUNT_KO.get(a, a), "currency": "USDT" if a == "BYBIT" else "USD",
            "this": _brief(this), "has_money": bool(acct_has_money[a]),
            "trend": [{"week": _wlabel(w), **_brief(acct_wk[a].get(w) or _new_stat())} for w in trend],
            "summary": "",
        })
    return {
        "label": label, "ws": ws, "we": we, "now": now, "partial": partial,
        "range": f"{ws:%m-%d} ~ {last_day:%m-%d}", "month_label": month_start.strftime("%Y-%m"),
        "rows": len(rows), "trend_labels": [_wlabel(w) for w in trend],
        "accounts": accounts, "cells": cells, "llm_note": "",
    }


# ───────────────────────────────────────────────────────────
# 2) 평가 (구독 Claude, 계좌별 1콜)
# ───────────────────────────────────────────────────────────
def evaluate(ctx: dict) -> None:
    import llm
    ok, why = llm.claude_available()
    ctx["llm_note"] = f"Claude {llm.model_for('weekly_eval')}" if ok else f"OpenAI 폴백({why})"
    by_acct = defaultdict(list)
    for c in ctx["cells"]:
        by_acct[c["account"]].append(c)
    for acc in ctx["accounts"]:
        a = acc["account"]
        cells = by_acct.get(a) or []
        if not cells:
            continue
        payload = {
            "week": ctx["label"], "range_kst": ctx["range"], "week_in_progress": ctx["partial"],
            "account": a, "account_desc": acc["name"],
            "cells": [{k: v for k, v in c.items() if k not in ("grade", "reason", "action", "recent")}
                      for c in cells],
        }
        try:
            data = llm.structured("weekly_eval", json.dumps(payload, ensure_ascii=False),
                                  _eval_schema(), system=EVAL_SYSTEM)
        except Exception as e:  # noqa: BLE001
            log.exception("weekly_eval 실패 account=%s", a)
            acc["summary"] = f"⚠️ 평가 실패: {str(e)[:200]}"
            continue
        by_key = {str(x.get("key")): x for x in (data.get("cells") or []) if isinstance(x, dict)}
        for c in cells:
            ev = by_key.get(c["key"])
            if ev and ev.get("grade") in GRADE_ORDER:
                c["grade"] = ev["grade"]
                c["reason"] = _sanitize(ev.get("reason"))
                c["action"] = _sanitize(ev.get("action"))
        acc["summary"] = _sanitize(data.get("summary"))


# ───────────────────────────────────────────────────────────
# 3) 렌더 (md) · data
# ───────────────────────────────────────────────────────────
def _grade_counts(cells: list[dict]) -> dict:
    cnt = {g: 0 for g in GRADE_ORDER}
    for c in cells:
        if c["grade"] in cnt:
            cnt[c["grade"]] += 1
    return cnt


def _counts_line(cnt: dict) -> str:
    return " · ".join(f"{GRADE_ICON[g]} {g} {cnt[g]}" for g in GRADE_ORDER)


def _sort_key(c: dict):
    return (GRADE_ORDER.index(c["grade"]) if c["grade"] in GRADE_ORDER else 9,
            segment_order(c["account"]), -abs(c["this"]["sum_pct"]))


def render_md(ctx: dict) -> str:
    cells, accounts = ctx["cells"], ctx["accounts"]
    graded = any(c["grade"] for c in cells)
    cnt = _grade_counts(cells)
    out = []
    out.append(f"# 트레이딩봇 주간 보고서 — {ctx['label']} ({ctx['range']}){' · 진행중' if ctx['partial'] else ''}")
    out.append(f"\n생성: {ctx['now']:%Y-%m-%d %H:%M} KST · 원천: Supabase trade_records {ctx['rows']}행({LOOKBACK_WEEKS}주 창)"
               + (f" · 평가: {ctx['llm_note']}" if ctx["llm_note"] else "")
               + (f" · ⚠️ 주 미완료({ctx['now']:%m-%d %H:%M} 기준)" if ctx["partial"] else "") + "\n")

    # ① 한눈에
    out.append("## 한눈에\n")
    for acc in accounts:
        t = acc["this"]
        wr = "" if t["win_rate"] is None else f"승률 {t['win_rate']:.0f}% · "
        out.append(f"- **{acc['name']}**: 진입 {t['entries']} · 청산 {t['exits']}건 · {wr}합계 {t['sum_pct']:+.1f}%p"
                   f" · 실현 {_money(acc['account'], t['realized'], acc['has_money'])}")
    if graded:
        out.append(f"- 판정: {_counts_line(cnt)} (셀 {len(cells)})")
    else:
        out.append(f"- 판정: 없음(평가 생략) · 셀 {len(cells)}")
    for acc in accounts:
        arrow = " → ".join(f"{x['sum_pct']:+.1f}" for x in reversed(acc["trend"]))
        out.append(f"- {acc['name']} 4주 합계%p: {arrow}")

    # ② 지금 볼 것
    out.append("\n## 지금 볼 것\n")
    hot = sorted([c for c in cells if c["grade"] in ("점검", "주의")], key=_sort_key)
    if hot:
        for c in hot:
            out.append(f"- {GRADE_ICON[c['grade']]} **{_cell_name(c)}** ({ACCOUNT_KO.get(c['account'], c['account'])}) "
                       f"· {_stat_line(c)} — {c['reason']} → {c['action']}")
    elif graded:
        out.append("- 없음 — 점검·주의 셀 없음")
    else:
        auto = [c for c in cells if c["mtd_flag"] or (c["streak"] >= 2 and not c["this"]["exits"])]
        for c in sorted(auto, key=lambda c: (c["mtd_flag"] is None, c["account"])):
            why = c["mtd_flag"] or f"무진입 {c['streak']}주"
            out.append(f"- ⚪ **{_cell_name(c)}** ({ACCOUNT_KO.get(c['account'], c['account'])}) · {_stat_line(c)} — {why} (자동)")
        if not auto:
            out.append("- 없음")

    # ③ 총평
    if any(a["summary"] for a in accounts):
        out.append("\n## 총평\n")
        for acc in accounts:
            if acc["summary"]:
                out.append(f"- **{acc['name']}**: {acc['summary']}")

    # ④ 관찰·정상 셀
    rest = sorted([c for c in cells if c["grade"] not in ("점검", "주의")], key=_sort_key)
    if rest:
        out.append("\n## 관찰 · 정상 셀\n" if graded else "\n## 셀 목록\n")
        for acc in accounts:
            mine = [c for c in rest if c["account"] == acc["account"]]
            if not mine:
                continue
            out.append(f"### {acc['name']}\n")
            for c in mine:
                icon = GRADE_ICON.get(c["grade"], "▫️")
                tail = f" · {c['reason']}" if c["reason"] else ""
                out.append(f"- {icon} **{_cell_name(c)}** · {_stat_line(c)}{tail}")
            out.append("")

    # ⑤ 월 누적 후보
    out.append(f"## 월 누적 🔴/🟡 후보 ({ctx['month_label']} 1일 ~ {ctx['range'].split('~')[-1].strip()})\n")
    flagged = [c for c in cells if c["mtd_flag"]]
    if flagged:
        for c in sorted(flagged, key=lambda c: c["mtd"]["sum_pct"]):
            m = c["mtd"]
            wr = "" if m["win_rate"] is None else f", 승률 {m['win_rate']:.0f}%"
            out.append(f"- {c['mtd_flag']} {c['account']} {_cell_name(c)}: 월누적 {m['sum_pct']:+.1f}%p (n={m['exits']}{wr})")
    else:
        out.append("- 없음 — 월누적 기준 전 셀 기준 내(청산 3건 이상 셀 대상)")

    # ⑥ 숫자표
    out.append("\n## 숫자표 (청산 기준)\n")
    out.append("| 셀 | 이번주 | 미청산 | 4주 | 월누적 | 무진입 |")
    out.append("|---|---|---|---|---|---|")
    for c in sorted(cells, key=lambda c: -c["this"]["sum_pct"]):
        t, r, m = c["this"], c["recent"], c["mtd"]
        tw = f"{t['exits']}건 {t['win_rate']:.0f}% {t['sum_pct']:+.1f}" if t["exits"] else (f"진입 {t['entries']}" if t["entries"] else "")
        out.append(f"| {c['account']} {_cell_name(c)} | {tw} | {t['open'] or ''} | "
                   f"{r['sum_pct']:+.1f} ({r['exits']}) | {m['sum_pct']:+.1f} ({m['exits']}) | "
                   f"{(str(c['streak']) + '주') if c['streak'] else ''} |")
    out.append("\n> MT5 금액은 2026-09-18 브로커 원장(딜) 기준 정정 이후 값. %는 진입/청산가 기반(수수료 0.11% 차감). "
               "판단 가이드: 주간은 조기 경보용 — 파라미터 변경은 월간 보고서(🔴 2개월 연속 / 백테스트 최악연도 초과)에서만. "
               "'점검'은 운영 결함(피드·청산) 의심이라 즉시 확인, '주의'는 월간 재검토 후보.")
    return "\n".join(out)


def to_data(ctx: dict) -> dict:
    cnt = _grade_counts(ctx["cells"])
    return {
        "kind": "weekly", "label": ctx["label"], "range": ctx["range"], "partial": ctx["partial"],
        "generated_at": ctx["now"].isoformat(timespec="seconds"), "month_label": ctx["month_label"],
        "llm": ctx["llm_note"], "trend_labels": ctx["trend_labels"],
        "grade_counts": cnt, "accounts": ctx["accounts"],
        "cells": sorted(ctx["cells"], key=_sort_key),
    }


def subtitle_of(ctx: dict) -> str:
    cnt = _grade_counts(ctx["cells"])
    graded = any(c["grade"] for c in ctx["cells"])
    parts = [ctx["range"]]
    parts.append(" ".join(f"{GRADE_ICON[g]}{cnt[g]}" for g in GRADE_ORDER) if graded else "평가 없음")
    if ctx["partial"]:
        parts.append("진행중")
    return " · ".join(parts)


def build(label: str, ws: datetime, we: datetime, *, use_llm: bool = True) -> tuple[str, dict, str]:
    ctx = collect(label, ws, we)
    if use_llm and ctx["cells"]:
        evaluate(ctx)
    return render_md(ctx), to_data(ctx), subtitle_of(ctx)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_llm = "--no-llm" not in sys.argv
    label, ws, we = parse_week(args[0] if args else None)
    md, data, subtitle = build(label, ws, we, use_llm=use_llm)
    if "--publish" in sys.argv:
        from report_store import publish_report
        publish_report("weekly", label, md, subtitle=subtitle, data=data)
        print(f"published weekly:{label} ({len(md)} chars, cells={len(data['cells'])})", file=sys.stderr)
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, default=str))
    else:
        print(md)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    main()

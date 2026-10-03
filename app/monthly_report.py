# -*- coding: utf-8 -*-
"""트레이딩봇 월간 성과 보고서 (2026-09-02 도입) — 매월 심볼×전략(셀) 평가·파라미터 조정 판단용.
데이터: Supabase trade_records(영구 아카이브, persist.py가 매일 적재) — Redis 보존한계 무관.
출력: 마크다운(stdout) — 셀별 실측(건수·승률·건당%·합계) vs 백테스트 기대, 조정 검토 플래그.
usage: python monthly_report.py [YYYY-MM]   (기본: 지난달)
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from persist import get_supabase

KST = timezone(timedelta(hours=9))

# 책 구분: signal_ns → 책 라벨 (프론트 bookOf 미러)
def book_of(ns):
    n = (ns or "").lower()
    if n.startswith("s11"):
        return "1분"
    if n.startswith("s22"):
        return "4h"
    if n in ("bybit", "mt5", "fxd", "mt5d", "cryptod"):
        return "일봉"
    return "?"

# 구분(세그먼트): 바이비트-크립토 / MT5-비환율(지수·금속·원유·코인CFD) / MT5-환율 (2026-10-03).
# 지금은 MT5 한 계좌에 섞여 있지만 나중에 환율·비환율을 별도 계좌로 떼어 낼 예정이라 보고서는 미리 나눠 본다.
# 키가 곧 표시명 — 사이트/앱은 모르는 account 키를 그대로 보여주므로 프론트 수정 없이 읽힌다.
_CCY = {"USD", "EUR", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF"}
SEGMENTS = ["BYBIT", "MT5-비환율", "MT5-환율"]
SEGMENT_KO = {"BYBIT": "크립토(Bybit)", "MT5": "MT5 계좌 합계", "MT5-비환율": "비환율(MT5)", "MT5-환율": "환율(MT5)"}


def segment_of(account, sym):
    if account != "MT5":
        return account
    s = str(sym or "").upper().lstrip("#").split(".")[0]
    if len(s) == 6 and s[:3] in _CCY and s[3:] in _CCY:
        return "MT5-환율"
    return "MT5-비환율"


def segment_order(seg):
    return SEGMENTS.index(seg) if seg in SEGMENTS else len(SEGMENTS)


# 백테스트 기대값(건당 %, 검증 기록 있는 셀만 — 없는 셀은 "-").
# 갱신처: reversion_research 검증 메모/HANDOFF. (참고용 — 판단은 세션에서)
EXPECTED = {
    ("US500", "1분", "S12"): 1.40, ("US500", "4h", "S12"): 2.04, ("US500", "4h", "S11"): 0.83,
    ("XAUTUSDT", "4h", "S13"): 1.4,   # PAXG/빗파 평균권
    ("US100", "1분", "S11"): 0.34,    # 재스크리닝 이웃 중앙
    ("XAUUSD", "1분", "S11"): 0.19,   # 라이브창 실측대
    ("XAUTUSDT", "1분", "S11"): 0.24,
}


def month_range(arg):
    if arg:
        y, m = map(int, arg.split("-"))
    else:
        first_this = datetime.now(KST).replace(day=1)
        prev = first_this - timedelta(days=1)
        y, m = prev.year, prev.month
    start = f"{y:04d}-{m:02d}-01"
    end_y, end_m = (y + 1, 1) if m == 12 else (y, m + 1)
    end = f"{end_y:04d}-{end_m:02d}-01"
    return f"{y:04d}-{m:02d}", start, end


def fetch_rows(sb, start, end):
    rows, page = [], 0
    while True:  # PostgREST 1000행 상한 → range 페이지네이션 필수
        d = (sb.table("trade_records").select("id, day, symbol, side, kind, signal, pnl, raw_json")
             .gte("day", start).lt("day", end).order("id")
             .range(page * 1000, page * 1000 + 999).execute().data or [])
        rows.extend(d)
        if len(d) < 1000:
            break
        page += 1
    return rows


def main():
    label, start, end = month_range(sys.argv[1] if len(sys.argv) > 1 else None)
    sb = get_supabase()
    rows = fetch_rows(sb, start, end)

    cells = defaultdict(lambda: dict(entries=0, exits=0, wins=0, pcts=[], usdt=0.0, has_usdt=False))
    acct_sum = defaultdict(lambda: dict(entries=0, exits=0, usdt=0.0, pcts=[]))

    for r in rows:
        raw = r.get("raw_json") or {}
        if not isinstance(raw, dict):
            raw = {}
        account = str(raw.get("account") or "BYBIT")
        sym = str(r.get("symbol") or "?")
        ns = raw.get("signal_ns") or ""
        tag = str(raw.get("strategy_tag") or (str(r.get("signal") or "").split("_")[0]) or "?").upper()
        if not tag.startswith("S"):
            tag = "?"
        book = book_of(ns)
        seg = segment_of(account, sym)
        key = (seg, sym, book, tag)
        kind = str(r.get("kind") or "").upper()
        if kind == "ENTRY":
            cells[key]["entries"] += 1
            acct_sum[seg]["entries"] += 1
        elif kind == "EXIT":
            c = cells[key]
            c["exits"] += 1
            acct_sum[seg]["exits"] += 1
            try:  # 가격 기반 % (MT5 pnl_usdt 스케일 문제 회피 — 방향 반영)
                ep, xp = float(raw.get("entry_price") or 0), float(raw.get("exit_price") or 0)
                if ep > 0 and xp > 0:
                    pct = (xp / ep - 1) * 100
                    if str(r.get("side") or "").upper() == "SHORT":
                        pct = -pct
                    pct -= 0.11  # 수수료 왕복 근사
                    c["pcts"].append(pct)
                    acct_sum[seg]["pcts"].append(pct)
                    if pct > 0:
                        c["wins"] += 1
            except (TypeError, ValueError):
                pass
            if account == "BYBIT":  # 금액은 BYBIT만 신뢰(MT5 계약크기 미반영)
                try:
                    v = float(r.get("pnl"))
                    c["usdt"] += v
                    c["has_usdt"] = True
                    acct_sum[seg]["usdt"] += v
                except (TypeError, ValueError):
                    pass

    out = []
    out.append(f"# 트레이딩봇 월간 보고서 — {label}")
    out.append(f"\n생성: {datetime.now(KST):%Y-%m-%d %H:%M} KST · 원천: Supabase trade_records {len(rows)}행\n")

    out.append("## 1. 구분별 요약 (MT5는 비환율·환율 분리)\n")
    out.append("| 구분 | 진입 | 청산 | 청산 합계% | 실현 USDT |")
    out.append("|---|---|---|---|---|")
    for a in sorted(acct_sum, key=segment_order):
        s = acct_sum[a]
        tot = sum(s["pcts"])
        usdt = f"{s['usdt']:+.2f}" if a == "BYBIT" else "—"
        out.append(f"| {SEGMENT_KO.get(a, a)} | {s['entries']} | {s['exits']} | {tot:+.1f}%p | {usdt} |")

    out.append("\n## 2. 셀별 실측 (청산 기준)\n")
    out.append("| 구분 | 심볼 | 책 | 태그 | 진입 | 청산 | 승률 | 건당% | 합계%p | 기대% | 판정 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    flags = []
    for key in sorted(cells, key=lambda k: -sum(cells[k]["pcts"])):
        a, sym, book, tag = key
        c = cells[key]
        n = c["exits"]
        avg = (sum(c["pcts"]) / len(c["pcts"])) if c["pcts"] else None
        tot = sum(c["pcts"])
        wr = (100 * c["wins"] / len(c["pcts"])) if c["pcts"] else None
        exp = EXPECTED.get((sym, book, tag))
        verdict = ""
        if n >= 3 and avg is not None:
            if tot < -3:
                verdict = "🔴 월손실"
                flags.append(f"- 🔴 {a} {sym} {book} {tag}: 합계 {tot:+.1f}%p (n={n}, 승률 {wr:.0f}%) — 파라미터 재검토 후보")
            elif wr is not None and wr < 40:
                verdict = "🟡 저승률"
                flags.append(f"- 🟡 {a} {sym} {book} {tag}: 승률 {wr:.0f}% (n={n}) — 관찰 유지")
            elif exp and avg < exp * 0.3:
                verdict = "🟡 기대미달"
                flags.append(f"- 🟡 {a} {sym} {book} {tag}: 건당 {avg:+.2f}% vs 기대 {exp:+.2f}% — 관찰 유지")
            else:
                verdict = "✅"
        elif n > 0:
            verdict = "표본부족"
        out.append(f"| {a} | {sym} | {book} | {tag} | {c['entries']} | {n} | "
                   f"{'' if wr is None else f'{wr:.0f}%'} | {'' if avg is None else f'{avg:+.2f}%'} | "
                   f"{tot:+.1f} | {'' if not exp else f'{exp:+.2f}'} | {verdict} |")

    out.append("\n## 3. 조정 검토 대상\n")
    out.extend(flags if flags else ["- 없음 — 전 셀 기준 내 동작"])
    out.append("\n> 판단 가이드: 🔴=재스윕/축소 검토(단, 1개월 표본은 노이즈 큼 — 2개월 연속 🔴이거나 백테스트 최악연도 초과 시 행동), "
               "🟡=관찰, 무거래 셀은 발동조건 미도달이면 정상. 파라미터 변경은 반드시 재검증(연도감사+이웃) 후 적용.")
    print("\n".join(out))


if __name__ == "__main__":
    main()

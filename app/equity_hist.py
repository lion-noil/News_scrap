# -*- coding: utf-8 -*-
"""평가액 이력 일별 요약 (2026-10-05) — 평가손 포함 낙폭·하루 최대 낙폭(프랍 규칙 대조)의 원천.

원천: 실행기가 갱신 주기마다 쌓는 Redis 스트림 trading:<ns>:equity_hist {ts_ms, equity, wallet}
  (MT5 두 계좌 60초, Bybit 300초 — tradingBot app/local_executor.py save_equity_sample, Redis 보존 40일).
요약: 하루(DAY_START_HOUR KST 경계 — 프랍 서버 자정과 비슷한 시각) 한 줄을 Upstash 해시 trading:<ns>:equity_days 에 영구 보관.
  값은 전부 '그날 시작 평가액 대비 %' 라 입출금·기록 공백에 영향받지 않는다.
  입출금 = 잔고와 평가액이 같은 폭으로 같이 뛴 구간(청산은 잔고만 변하고 평가액은 그대로) → 낙폭 계산에서 제외.
usage: python equity_hist.py [일수]   (최근 N일 다시 요약, 기본 3 — perf_report 가 발행 전에 호출)
"""
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from persist import news_redis, redis_client

log = logging.getLogger("equity_hist")
KST = timezone(timedelta(hours=9))
DAY_START_HOUR = 7      # KST 07:00 = 프랍(FTMO 등, CE(S)T 자정) 일일 낙폭 초기화 시각과 같거나 1시간 차
ACCOUNTS = {            # 성적표 구분 → 실행기 상태 ns
    "BYBIT": "agent:CopyZannavi:u7c9f14d2a1:BYBIT",
    "MT5-비환율": "agent:CopyZannaviMT5:u8f3a9c1e7b:MT5",
    "MT5-환율": "agent:CopyZannaviFX:u5d2b7e4c9a:MT5",
}


def _s(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def day_of(ts_ms: int) -> str:
    return (datetime.fromtimestamp(ts_ms / 1000, KST) - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


def _samples(ns: str, since_ms: int) -> list:
    out = []
    for _id, fields in redis_client.xrange(f"trading:{ns}:equity_hist", min=f"{since_ms}-0", max="+"):
        f = {_s(k): _s(v) for k, v in fields.items()}
        try:
            eq = float(f["equity"])
            out.append((int(f.get("ts_ms") or str(_s(_id)).split("-")[0]), eq, float(f.get("wallet") or eq)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def rollup(ns: str, back_days: int = 3) -> int:
    """최근 back_days 일을 다시 요약해 저장(덮어쓰기, 멱등). 반환 = 저장한 날 수."""
    now = datetime.now(KST)
    start = (now - timedelta(hours=DAY_START_HOUR)).replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=back_days - 1) + timedelta(hours=DAY_START_HOUR)
    rows = _samples(ns, int(start.timestamp() * 1000))
    days, flow_cum, prev = {}, 0.0, None
    for ts, eq, w in rows:
        if prev is not None:
            dw, de = w - prev[2], eq - prev[1]
            if abs(dw) > max(0.002 * prev[1], 1.0) and abs(de - dw) < 0.3 * abs(dw):
                flow_cum += dw          # 입출금: 이후 평가액에서 빼고 본다
        prev = (ts, eq, w)
        adj = eq - flow_cum
        d = days.setdefault(day_of(ts), {"open": adj, "high": adj, "low": adj, "peak": adj, "intra": 0.0,
                                         "eq_open": eq, "flow0": flow_cum, "n": 0})
        d["high"], d["low"], d["close"], d["eq_close"], d["n"] = max(d["high"], adj), min(d["low"], adj), adj, eq, d["n"] + 1
        d["peak"] = max(d["peak"], adj)
        d["intra"] = max(d["intra"], d["peak"] - adj)      # 그날 안 고점 대비 최대 하락(순서 반영)
        d["flow1"] = flow_cum
    n = 0
    for day, d in days.items():
        o = d["open"]
        if not o:
            continue
        payload = {
            "open": round(d["eq_open"], 2), "close": round(d["eq_close"], 2), "n": d["n"],
            "ret_pct": round((d["close"] / o - 1) * 100, 3),
            "high_pct": round((d["high"] / o - 1) * 100, 3), "low_pct": round((d["low"] / o - 1) * 100, 3),
            "day_dd_pct": round(max(0.0, (o - d["low"]) / o * 100), 3),      # 프랍 일일 낙폭: 그날 시작 대비 최저
            "intra_dd_pct": round(d["intra"] / o * 100, 3),
            "flow": round(d["flow1"] - d["flow0"], 2),
        }
        news_redis.hset(f"trading:{ns}:equity_days", day, json.dumps(payload))
        n += 1
    return n


def load_days(ns: str) -> dict:
    out = {}
    for k, v in (news_redis.hgetall(f"trading:{ns}:equity_days") or {}).items():
        try:
            out[_s(k)] = json.loads(_s(v))
        except Exception:  # noqa: BLE001
            continue
    return out


def month_stats(days: dict) -> dict:
    """{ym: {mtm_dd, day_dd_max, days}} — 월초 100 에서 일별 수익률을 이어 붙인 곡선의 고점 대비 최대 하락(평가손 포함)."""
    out = {}
    for day in sorted(days):
        d, ym = days[day], day[:7]
        m = out.setdefault(ym, {"idx": 100.0, "peak": 100.0, "mtm_dd": 0.0, "day_dd_max": 0.0, "days": 0})
        o = m["idx"]
        low, high = o * (1 + d["low_pct"] / 100), o * (1 + d["high_pct"] / 100)
        m["mtm_dd"] = max(m["mtm_dd"], (m["peak"] - low) / m["peak"] * 100, d.get("intra_dd_pct", 0.0) * o / m["peak"])
        m["peak"] = max(m["peak"], high)
        m["idx"] = o * (1 + d["ret_pct"] / 100)
        m["day_dd_max"] = max(m["day_dd_max"], d["day_dd_pct"])
        m["days"] += 1
    return {ym: {"mtm_dd": round(m["mtm_dd"], 2), "day_dd_max": round(m["day_dd_max"], 2), "days": m["days"]}
            for ym, m in out.items()}


def rollup_all(back_days: int = 3) -> None:
    for seg, ns in ACCOUNTS.items():
        try:
            log.info("equity_days %s: %d일 요약", seg, rollup(ns, back_days))
        except Exception as e:  # noqa: BLE001
            log.warning("equity_days %s 요약 실패: %s", seg, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    rollup_all(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
    for seg, ns in ACCOUNTS.items():
        print(seg, json.dumps(month_stats(load_days(ns)), ensure_ascii=False))

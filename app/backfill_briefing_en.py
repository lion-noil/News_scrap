# backfill_briefing_en.py — 과거 시장 브리핑(market_briefings 해시)에 영문 번역 en 채우기 (2026-10-09)
#
# 왜: 2026-10-09 부터 시장브리핑.py 가 en 을 함께 저장하지만 그 전 날짜(09-25~)는 비어 있어 /en/briefing/<day> 가 없다.
# 동작: 해시의 모든 날짜 중 (a) en 이 없는 항목 → 전체 번역(translate_briefing_en, LLM 1회/건, 본문+뉴스, 숫자 백스톱 포함)
#       (b) en 은 있지만 en.news 가 없는 항목 → 뉴스만 번역(translate_news_en, LLM 1회/건)해 기존 en 에 병합(본문 재번역 없음).
#       날짜 내림차순, 같은 키에 덮어쓴다. market_briefings:latest 는 건드리지 않는다.
#       저장된 날짜들은 끝에 IndexNow 로 /en/briefing/<day> 를 한 번에 핑.
# 뉴스 원본: news:daily_briefing:YYYYMMDD(전일브리핑 items, Redis TTL 30일 — Supabase 미보존). 만료된 날은 브리핑 JSON 의
#       news_items(title·category만)로 제목만 번역하고 summary 는 빈 문자열. 원본조차 없으면 news=[] 로 저장(LLM 호출 없음).
# 실행(컨테이너 안, WORKDIR=/app):
#   docker exec news-scrap python backfill_briefing_en.py            # en 없는 날 전체 번역 + en.news 없는 날 뉴스만
#   docker exec news-scrap python backfill_briefing_en.py --dry      # 대상 목록만(LLM 호출·저장 없음) — 종류(full/news) 표시
#   docker exec news-scrap python backfill_briefing_en.py --days 14  # 최근 14일만
#   docker exec news-scrap python backfill_briefing_en.py --day 2026-10-01 --force   # 특정 날짜 전체 재번역(news 포함)
#   --no-ping: IndexNow 핑 생략
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 어느 cwd 에서 실행해도 app/ 모듈 import

from redis_client import redis_client  # noqa: E402
import indexnow  # noqa: E402
from 시장브리핑 import HASH, SEOUL, translate_briefing_en, translate_news_en  # noqa: E402

log = logging.getLogger("backfill_briefing_en")


def _dec(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def load_all() -> dict[str, dict]:
    out = {}
    for k, v in (redis_client.hgetall(HASH) or {}).items():
        day = _dec(k)
        try:
            out[day] = json.loads(_dec(v))
        except Exception as e:
            log.warning("⚠️ %s JSON 파싱 실패 — 건너뜀: %s", day, e)
    return out


def plan_kind(b: dict, force: bool) -> str | None:
    """이 브리핑에 필요한 작업: 'full'(en 없음 또는 --force) · 'news'(en 은 있고 news 키 없음) · None(할 일 없음)."""
    en = b.get("en")
    if force or not isinstance(en, dict) or not en:
        return "full"
    if "news" not in en:
        return "news"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="market_briefings 영문 번역(en) 백필 — 본문+뉴스 또는 뉴스만")
    ap.add_argument("--dry", action="store_true", help="대상 목록만 출력(LLM 호출·저장 없음)")
    ap.add_argument("--days", type=int, default=0, help="최근 N일만(0=전부)")
    ap.add_argument("--day", help="특정 날짜(YYYY-MM-DD) 하나만")
    ap.add_argument("--force", action="store_true", help="en 이 이미 있어도 전체(본문+뉴스) 다시 번역")
    ap.add_argument("--no-ping", action="store_true", help="IndexNow 핑 생략")
    args = ap.parse_args()

    allb = load_all()
    days = sorted(allb.keys(), reverse=True)
    if args.day:
        days = [d for d in days if d == args.day]
    if args.days > 0:
        cutoff = (datetime.now(SEOUL).date() - timedelta(days=args.days)).isoformat()
        days = [d for d in days if d >= cutoff]
    plan = {d: k for d in days if (k := plan_kind(allb[d], args.force))}
    targets = list(plan.keys())
    n_full = sum(1 for k in plan.values() if k == "full")
    log.info("📚 브리핑 %d건 중 대상 %d건 (full=%d news-only=%d, dry=%s force=%s days=%s)",
             len(allb), len(targets), n_full, len(targets) - n_full, args.dry, args.force, args.days or "all")
    if not targets:
        print("(대상 없음)")
        return 0
    if args.dry:
        for d in targets:
            print(f"{d}  [{plan[d]}]  {allb[d].get('title', '')}")
        return 0

    done, failed = [], []
    for d in targets:
        b = allb[d]
        kind = plan[d]
        try:
            if kind == "full":
                en = translate_briefing_en(b)
                b["en"] = en
            else:
                news = translate_news_en(b)
                en = dict(b["en"])
                en["news"] = news
                b["en"] = en
        except Exception as e:
            log.warning("❌ %s %s 번역 실패: %s", d, kind, e)
            failed.append(d)
            continue
        redis_client.hset(HASH, d, json.dumps(b, ensure_ascii=False))
        done.append(d)
        n_news = len(en.get("news") or [])
        n_sum = sum(1 for n in (en.get("news") or []) if n.get("summary"))
        if kind == "full":
            log.info("✅ %s en 저장(full) numbers_ok=%s news=%d건(요약 %d) — %s", d, en.get("numbers_ok"), n_news, n_sum, en.get("title"))
        else:
            log.info("✅ %s en.news 병합(news-only) news=%d건(요약 %d) — %s", d, n_news, n_sum, en.get("title"))

    if done and not args.no_ping:
        indexnow.ping([f"/en/briefing/{d}" for d in done] + ["/en/briefing", "/briefing"])
    print(f"완료 {len(done)}건, 실패 {len(failed)}건" + (f" (실패: {', '.join(failed)})" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())

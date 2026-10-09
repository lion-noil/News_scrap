# backfill_briefing_en.py — 과거 시장 브리핑(market_briefings 해시)에 영문 번역 en 채우기 (2026-10-09)
#
# 왜: 2026-10-09 부터 시장브리핑.py 가 en 을 함께 저장하지만 그 전 날짜(09-25~)는 비어 있어 /en/briefing/<day> 가 없다.
# 동작: 해시의 모든 날짜 중 en 이 없는 항목을 날짜 내림차순으로 번역(LLM 1회/건, 숫자 백스톱 포함)해 같은 키에 덮어쓴다.
#       market_briefings:latest 는 건드리지 않는다. 저장된 날짜들은 끝에 IndexNow 로 /en/briefing/<day> 를 한 번에 핑.
# 실행(컨테이너 안, WORKDIR=/app):
#   docker exec news-scrap python backfill_briefing_en.py            # en 없는 전부
#   docker exec news-scrap python backfill_briefing_en.py --dry      # 대상 목록만(LLM 호출·저장 없음)
#   docker exec news-scrap python backfill_briefing_en.py --days 14  # 최근 14일만
#   docker exec news-scrap python backfill_briefing_en.py --day 2026-10-01 --force   # 특정 날짜 재번역
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
from 시장브리핑 import HASH, SEOUL, translate_briefing_en  # noqa: E402

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


def main() -> int:
    ap = argparse.ArgumentParser(description="market_briefings 영문 번역(en) 백필")
    ap.add_argument("--dry", action="store_true", help="대상 목록만 출력(LLM 호출·저장 없음)")
    ap.add_argument("--days", type=int, default=0, help="최근 N일만(0=전부)")
    ap.add_argument("--day", help="특정 날짜(YYYY-MM-DD) 하나만")
    ap.add_argument("--force", action="store_true", help="en 이 이미 있어도 다시 번역")
    ap.add_argument("--no-ping", action="store_true", help="IndexNow 핑 생략")
    args = ap.parse_args()

    allb = load_all()
    days = sorted(allb.keys(), reverse=True)
    if args.day:
        days = [d for d in days if d == args.day]
    if args.days > 0:
        cutoff = (datetime.now(SEOUL).date() - timedelta(days=args.days)).isoformat()
        days = [d for d in days if d >= cutoff]
    targets = [d for d in days if args.force or not allb[d].get("en")]
    log.info("📚 브리핑 %d건 중 대상 %d건 (dry=%s force=%s days=%s)", len(allb), len(targets), args.dry, args.force, args.days or "all")
    if not targets:
        print("(대상 없음)")
        return 0
    if args.dry:
        for d in targets:
            print(f"{d}  {allb[d].get('title', '')}")
        return 0

    done, failed = [], []
    for d in targets:
        b = allb[d]
        try:
            en = translate_briefing_en(b)
        except Exception as e:
            log.warning("❌ %s 번역 실패: %s", d, e)
            failed.append(d)
            continue
        b["en"] = en
        redis_client.hset(HASH, d, json.dumps(b, ensure_ascii=False))
        done.append(d)
        log.info("✅ %s en 저장 numbers_ok=%s — %s", d, en.get("numbers_ok"), en.get("title"))

    if done and not args.no_ping:
        indexnow.ping([f"/en/briefing/{d}" for d in done] + ["/en/briefing", "/briefing"])
    print(f"완료 {len(done)}건, 실패 {len(failed)}건" + (f" (실패: {', '.join(failed)})" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())

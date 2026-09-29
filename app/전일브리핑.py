# 전일브리핑.py
# 확정된 전날(06:50 윈도우) 각국 뉴스 요약(summary_result)을 모두 모아
# "핵심적으로 봐야 할 뉴스 5개"를 선별한다 — 여러 나라가 공통으로 주목한 사안 우선.
#
# 실행 시점: 06:55 scheduled_persist_supabase(전날 데이터 확정) 직후 매일.
#   예) 7/8 06:55 실행 → 7/7 06:50~7/8 06:50 윈도우(day="2026-07-07") 데이터로 브리핑.
#
# 입력:
#   - Supabase daily_collections day=전날 (raw_json.youtube_data[country].summary_result)
#   - (폴백) Redis news:daily_saved_data:YYYYMMDD 스냅샷
# 출력:
#   - Redis hset youtube_data["global_briefing"] = {date, generated_at, items[5]}
#     → 기존 /youtube 패스스루로 프론트 홈에 자동 전파 (새 API 불필요)
#   - Redis set news:daily_briefing:YYYYMMDD (TTL 30일, 히스토리)
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, time as dtime

from pytz import timezone
from dotenv import load_dotenv

import llm  # 구독 Claude(claude -p) → 실패 시 OpenAI 폴백 (2026-09-12)
from redis_client import redis_client

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SEOUL = timezone("Asia/Seoul")

# 세계정세분석.py / 프론트 newsParams와 동일 키
COUNTRIES = ["Korea", "USA", "Japan", "China", "Germany", "UK", "India", "HongKong"]

BRIEFING_PROMPT = (
    "너는 글로벌 뉴스 데스크의 수석 에디터다. 아래는 어제 하루 동안 각국 대표 뉴스 채널이 보도한 내용의 요약이다.\n"
    "이걸 종합해 '어제 세계에서 핵심적으로 봐야 할 뉴스 5개'를 선별하라.\n\n"
    "선별 기준(중요):\n"
    "1. 여러 나라 채널이 공통으로 다룬 사안을 최우선으로 뽑아라 — 그것이 글로벌 이슈다.\n"
    "2. 금융시장·지정학·거시경제에 파급이 큰 사안을 우선하라.\n"
    "3. 한 나라만 다뤘어도 파급이 명백히 크면 포함 가능.\n"
    "4. 같은 사안은 하나로 묶어라(중복 금지).\n"
    "   각 항목의 summary는 정확히 3줄(줄바꿈 \\n으로 구분). 각 줄은 '• '로 시작하는 한 문장, 공백 포함 45자 안팎.\n"
    "   1줄=무슨 일이 있었나, 2줄=핵심 수치·전개, 3줄=왜 중요한가(시장·안보 파급).\n"
    "   쉼표로 여러 사실을 이어 붙이지 말고, 한 줄에 사실 하나만. 3줄에 안 들어가는 곁가지는 버려라.\n"
    "5. countries에는 그 사안을 보도한 나라(입력에 실제 등장한 나라만)를 나열하라.\n"
    "반드시 한국어로, 정확히 5개를 rank 1(가장 중요)부터 순서대로 출력하라."
)


def _briefing_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "rank": {"type": "integer"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "countries": {"type": "array", "items": {"type": "string"}},
                        "category": {
                            "type": "string",
                            "enum": ["지정학", "경제·시장", "정치", "산업·기술", "사회·기타"],
                        },
                    },
                    "required": ["rank", "title", "summary", "countries", "category"],
                },
            }
        },
        "required": ["items"],
    }


# ───────────────────────────────────────────────────────────
# 1) 전날 하루치 수집
# ───────────────────────────────────────────────────────────
def _target_day(now=None) -> str:
    """직전 완료된 06:50 윈도우의 day(YYYY-MM-DD). persist_today_data와 동일 규칙."""
    now = now or datetime.now(SEOUL)
    today_0650 = SEOUL.localize(datetime.combine(now.date(), dtime(6, 50)))
    day_start = (today_0650 - timedelta(days=1)) if now >= today_0650 else (today_0650 - timedelta(days=2))
    return day_start.strftime("%Y-%m-%d")


def _extract(yd: dict) -> dict:
    out = {}
    for country, info in (yd or {}).items():
        if country in COUNTRIES and isinstance(info, dict) and info.get("summary_result"):
            out[country] = str(info["summary_result"])
    return out


def _collect_day_summaries(day: str) -> dict:
    """{country: summary_text} — Supabase 우선, Redis 스냅샷 폴백."""
    per_country = {}
    try:
        from persist import get_supabase
        supabase = get_supabase()
        resp = (supabase.table("daily_collections")
                .select("day, raw_json")
                .eq("day", day)
                .limit(1)
                .execute())
        rows = resp.data or []
        if rows:
            per_country = _extract((rows[0].get("raw_json") or {}).get("youtube_data", {}))
    except Exception as e:
        log.warning("⚠️ daily_collections(day=%s) 조회 실패, Redis 폴백: %s", day, e)

    if not per_country:
        raw = redis_client.get(f"news:daily_saved_data:{day.replace('-', '')}")
        if raw:
            try:
                per_country = _extract(json.loads(raw).get("youtube_data", {}))
            except Exception:
                pass

    # 3차 폴백: 라이브 youtube_data 해시에서 processed_time이 그 날 06:50 윈도우에 든 것만.
    # (23시 스냅샷/persist가 누락된 날에도 그날 처리된 나라만큼은 브리핑 가능)
    if not per_country:
        try:
            day_start = SEOUL.localize(datetime.strptime(day, "%Y-%m-%d").replace(hour=6, minute=50))
            day_end = day_start + timedelta(days=1)
            from pytz import utc as _utc
            live = redis_client.hgetall("youtube_data") or {}
            picked = {}
            for cb, jb in live.items():
                c = cb.decode() if isinstance(cb, (bytes, bytearray)) else str(cb)
                if c not in COUNTRIES:
                    continue
                try:
                    info = json.loads(jb.decode() if isinstance(jb, (bytes, bytearray)) else jb)
                    pt = _utc.localize(datetime.strptime(info["processed_time"], "%Y-%m-%dT%H:%M:%SZ")).astimezone(SEOUL)
                    if day_start <= pt < day_end and info.get("summary_result"):
                        picked[c] = str(info["summary_result"])
                except Exception:
                    continue
            if picked:
                log.info("ℹ️ 라이브 해시 폴백 사용 (day=%s): %s", day, list(picked.keys()))
                per_country = picked
        except Exception as e:
            log.warning("⚠️ 라이브 해시 폴백 실패: %s", e)
    return per_country


# ───────────────────────────────────────────────────────────
# 2) LLM 선별 + 저장
# ───────────────────────────────────────────────────────────
# 입력 국가가 이보다 적으면 '글로벌' 브리핑이 아니라 한 나라 뉴스의 재탕이 된다.
# (2026-09-17 실사고: 수집 루프 장애로 홍콩 1개국만 남았는데 3차 폴백이 그걸로 브리핑을 만들어
#  홈이 홍콩·중국 뉴스로 도배됨) → 생성하지 않고 직전 브리핑을 유지한다.
MIN_BRIEFING_COUNTRIES = 2


class BriefingInputTooThin(RuntimeError):
    pass


def generate_daily_briefing(day: str | None = None) -> dict:
    day = day or _target_day()
    per_country = _collect_day_summaries(day)
    if not per_country:
        raise RuntimeError(f"브리핑 입력 없음 (day={day})")
    if len(per_country) < MIN_BRIEFING_COUNTRIES:
        raise BriefingInputTooThin(
            f"브리핑 입력 국가 부족 (day={day}, countries={list(per_country)}, 최소 {MIN_BRIEFING_COUNTRIES})")

    blocks = [f"===== {c} 뉴스 채널 ({day}) =====\n{txt}" for c, txt in per_country.items()]
    log.info("🗞️ 브리핑 입력: day=%s countries=%s", day, list(per_country.keys()))

    # role=briefing — .env CLAUDE_MODEL_BRIEFING 로 모델 교체 가능
    data = llm.structured("briefing", "\n\n".join(blocks), _briefing_schema(), system=BRIEFING_PROMPT)
    items = data.get("items", [])
    items = sorted(items, key=lambda x: x.get("rank", 99))[:5]

    return {
        "date": day,
        "generated_at": datetime.now(SEOUL).isoformat(),
        "countries_in": list(per_country.keys()),  # 실제 입력된 국가 (부분 브리핑 판별용)
        "items": items,
    }


# ───────────────────────────────────────────────────────────
# 2-b) 오늘 브리핑(롤링) — 2026-09-19
#   06:55 최종본(전일)만 있으면 홈 브리핑이 하루 종일 '어제' 라벨이고 자정을 넘기면 '이틀 전'이 된다.
#   오늘(KST 달력일) 요약이 MIN_ROLLING_COUNTRIES 개국 이상 모이면 낮에도 홈 브리핑을 다시 만든다.
#   - 입력 기준은 save_daily_data 와 동일(processed_time 의 KST 달력일 == 오늘) → 06:55 최종본과 같은 재료.
#   - 히스토리 키(news:daily_briefing:*)는 쓰지 않는다(최종본만). 해시(global_briefing)만 갱신, rolling=True 표기.
#   - 입력 서명(sig)이 같으면 스킵, 직전 롤링 후 ROLLING_MIN_INTERVAL_MIN 안이면 스킵 → 하루 3~5회.
#   - 최소 4개국: 새벽(홍콩·인도·영국 3개국)에 전날 최종본을 밀어내지 않도록. 한국 뉴스광장(08시)까지 오면 시작.
# ───────────────────────────────────────────────────────────
MIN_ROLLING_COUNTRIES = int(os.getenv("ROLLING_MIN_COUNTRIES", "4"))
ROLLING_MIN_INTERVAL_MIN = int(os.getenv("ROLLING_MIN_INTERVAL_MIN", "120"))

ROLLING_PROMPT = (
    BRIEFING_PROMPT
    .replace("아래는 어제 하루 동안", "아래는 오늘 지금까지")
    .replace("'어제 세계에서 핵심적으로 봐야 할 뉴스 5개'", "'오늘 세계에서 핵심적으로 봐야 할 뉴스 5개'")
    + "\n일부 나라의 보도는 아직 들어오지 않았을 수 있다. 입력에 있는 나라의 보도만 근거로 삼고, 없는 나라를 추정하지 마라."
)


def _today_calendar() -> str:
    return datetime.now(SEOUL).strftime("%Y-%m-%d")


def _collect_live_today(day: str) -> tuple[dict, dict]:
    """live youtube_data 해시에서 processed_time 의 KST 달력일 == day 인 나라만.
    반환: ({country: summary_text}, {country: processed_time})"""
    from pytz import utc as _utc
    per_country, proc = {}, {}
    live = redis_client.hgetall("youtube_data") or {}
    for cb, jb in live.items():
        c = cb.decode() if isinstance(cb, (bytes, bytearray)) else str(cb)
        if c not in COUNTRIES:
            continue
        try:
            info = json.loads(jb.decode() if isinstance(jb, (bytes, bytearray)) else jb)
            pt = info.get("processed_time")
            d = _utc.localize(datetime.strptime(pt, "%Y-%m-%dT%H:%M:%SZ")).astimezone(SEOUL).strftime("%Y-%m-%d")
            if d == day and info.get("summary_result"):
                per_country[c] = str(info["summary_result"])
                proc[c] = pt
        except Exception:
            continue
    return per_country, proc


def _current_briefing() -> dict | None:
    try:
        raw = redis_client.hget("youtube_data", "global_briefing")
        if raw:
            return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        pass
    return None


def generate_and_store_rolling_briefing(force: bool = False) -> dict | None:
    """스케줄러(매시 수집 직후) 진입점. 조건 미달·변경 없음·간격 미만이면 None."""
    day = _today_calendar()
    per_country, proc = _collect_live_today(day)
    if len(per_country) < MIN_ROLLING_COUNTRIES:
        log.info("⏭️ 오늘 브리핑: 입력 %d개국 < %d — 스킵 (day=%s)", len(per_country), MIN_ROLLING_COUNTRIES, day)
        return None
    sig = "|".join(f"{c}:{proc[c]}" for c in sorted(per_country))
    cur = _current_briefing() or {}
    if cur.get("date") and day < str(cur.get("date")):
        log.info("⏭️ 오늘 브리핑: 해시가 더 최신 날짜(%s) — 스킵", cur.get("date"))
        return None
    if not force and cur.get("date") == day and cur.get("rolling"):
        if cur.get("sig") == sig:
            log.info("⏭️ 오늘 브리핑: 새 입력 없음 — 스킵 (day=%s, %d개국)", day, len(per_country))
            return None
        try:
            gen = datetime.fromisoformat(str(cur.get("generated_at")))
            if gen.tzinfo is None:
                gen = SEOUL.localize(gen)
            mins = (datetime.now(SEOUL) - gen).total_seconds() / 60
            if mins < ROLLING_MIN_INTERVAL_MIN:
                log.info("⏭️ 오늘 브리핑: 직전 갱신 %.0f분 전(<%d분) — 스킵", mins, ROLLING_MIN_INTERVAL_MIN)
                return None
        except Exception:
            pass

    blocks = [f"===== {c} 뉴스 채널 ({day}) =====\n{txt}" for c, txt in per_country.items()]
    log.info("🗞️ 오늘 브리핑 입력: day=%s countries=%s", day, list(per_country.keys()))
    # role=briefing_rolling — .env CLAUDE_MODEL_BRIEFING_ROLLING 로 모델 교체 가능
    data = llm.structured("briefing_rolling", "\n\n".join(blocks), _briefing_schema(), system=ROLLING_PROMPT)
    items = sorted(data.get("items", []), key=lambda x: x.get("rank", 99))[:5]
    briefing = {
        "date": day,
        "generated_at": datetime.now(SEOUL).isoformat(),
        "countries_in": list(per_country.keys()),
        "items": items,
        "rolling": True,
        "sig": sig,
    }
    redis_client.hset("youtube_data", "global_briefing", json.dumps(briefing, ensure_ascii=False))
    log.info("✅ 오늘 브리핑 저장 date=%s countries=%d items=%d", day, len(per_country), len(items))
    return briefing


def store_daily_briefing(briefing: dict):
    payload = json.dumps(briefing, ensure_ascii=False)
    # 홈 전파용 (기존 /youtube 패스스루) — save_daily_data는 processed_time 없는 필드를 걸러서 안전.
    # ⚠️ 과거 날짜 백필 시 최신 브리핑을 되돌리지 않도록, 저장 날짜가 현재 해시보다 과거면 해시는 건너뛴다
    #    (2026-08-09 실사고: 8/3 백필이 홈의 8/8 브리핑을 덮어씀). 히스토리 키는 항상 저장.
    skip_hash = False
    try:
        raw = redis_client.hget("youtube_data", "global_briefing")
        if raw:
            cur = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
            cur_date = str(cur.get("date") or "")
            if cur_date and str(briefing["date"]) < cur_date:
                skip_hash = True
                log.info("⏭️ 과거 날짜(%s < %s) 백필 — global_briefing 해시는 유지", briefing["date"], cur_date)
    except Exception:
        pass
    if not skip_hash:
        redis_client.hset("youtube_data", "global_briefing", payload)
    # 히스토리 (30일)
    redis_client.set(f"news:daily_briefing:{briefing['date'].replace('-', '')}", payload, ex=30 * 86400)
    log.info("✅ 전일 브리핑 저장 완료 date=%s items=%d", briefing["date"], len(briefing["items"]))


def briefing_already_done(day: str | None = None) -> bool:
    """해당 day 브리핑이 이미 있으면 True (재시작 시 LLM 재호출 방지)."""
    day = day or _target_day()
    try:
        # 최종본은 항상 히스토리 키를 쓴다 → 있으면 완료. (홈 해시가 롤링으로 더 최신 날짜를 가리킬 때
        #  해시만 보면 '미완료'로 오판해 재시작마다 옛 날짜 최종본을 다시 생성했음 — 2026-09-19 00:54 실측)
        if redis_client.exists(f"news:daily_briefing:{day.replace('-', '')}"):
            return True
        raw = redis_client.hget("youtube_data", "global_briefing")
        if raw:
            cur = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
            # 롤링(오늘 브리핑)은 최종본이 아니므로 '완료'로 치지 않는다 (2026-09-19)
            return cur.get("date") == day and not cur.get("rolling")
    except Exception:
        pass
    return False


def generate_and_store_daily_briefing(force: bool = False) -> dict | None:
    """스케줄러/수동 실행 진입점. 같은 날 중복 생성은 스킵(force=True로 강제)."""
    day = _target_day()
    if not force and briefing_already_done(day):
        log.info("⏭️ 전일 브리핑 이미 생성됨 (day=%s) — 스킵", day)
        return None
    try:
        briefing = generate_daily_briefing(day)
    except BriefingInputTooThin as e:
        log.warning("⚠️ %s — 직전 브리핑 유지", e)
        return None
    store_daily_briefing(briefing)
    return briefing


if __name__ == "__main__":
    out = generate_and_store_daily_briefing(force=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))

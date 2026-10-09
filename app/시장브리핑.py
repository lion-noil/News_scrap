# 시장브리핑.py — 어제 종가 시세(chart_data) + 전일 글로벌 브리핑(news) → "오늘의 시장 브리핑" 1편 (2026-09-25)
#
# 왜: noil 아카이브(날짜별 세계 뉴스 요약)는 검색 수요가 거의 없고(네이버 '세계뉴스' 1,810/월),
#     사람들이 찾는 건 '환율전망' 32,000 · '달러환율전망' 40,360 · '미국금리' 200,900 · '코스피전망' 7,100.
#     우리만 가진 조합 = 시세 데이터와 7개국 뉴스 요약이 같은 파이프라인에 있다는 것.
#     → 하루 한 편, 숫자가 들어간 검색형 제목의 브리핑을 /briefing/<날짜> 로 낸다.
#
# 실행: 06:55 persist → 세계정세 → 전일 브리핑 직후 run_market_briefing(main.py). 컨테이너 기동 시에도(같은 날 있으면 스킵).
#       시세 갱신이 없는 날(주말·휴장 다음 날)은 직전 브리핑과 data_date가 같으면 스킵 → 중복 글 방지.
# 입력:
#   - Redis chart_data[currency|treasury|index|commodity] (지수정보가져오기, 매시) → 마지막 두 종가·100일 평균
#   - Bybit 공개 API BTCUSDT 일봉(코인은 chart_data에 없음; 07시 현재가 기준)
#   - Redis news:daily_briefing:YYYYMMDD (전일브리핑 items[5]) + 한국·미국 요약(전일브리핑._collect_day_summaries)
# 출력:
#   - Redis hash market_briefings[YYYY-MM-DD] = JSON {day, title, lead, sections[], keywords[], movers[], snapshot[], news_items[], ...}
#   - Redis market_briefings:latest = YYYY-MM-DD
#   - (2026-10-09) 같은 JSON 안에 영문 번역 en = {title, lead, sections[{key,heading,body}], keywords[],
#     news[{title,category,summary}], numbers_ok, generated_at} — 사이트 /en/briefing/<day>.
#     번역은 LLM 1회(market_briefing_en, 본문+뉴스 5개 한 스키마), 실패 시 en 만 생략(한국어 저장은 그대로).
#     en.news 는 _news_input 의 뉴스 5개(제목·분류·요약) 영문판 — 숫자 백스톱은 title/lead/sections 에만 적용(뉴스엔 시세표 밖 숫자가 당연).
#     과거분은 backfill_briefing_en.py(en 없는 날=전체 번역, en 만 있고 news 없는 날=translate_news_en 경량 경로).
#   - (2026-10-09) 저장 직후 IndexNow 핑(indexnow.py): /briefing/<day>, /en/briefing/<day>, /briefing, /, /market/<slug>, /en/market/<slug>
#   → hyeongeonnoil/pages/briefing/[day].jsx(ISR) · 사이트맵 · RSS 가 읽는다.
# 규칙(유사투자자문 회피): 예측·매수/매도 권유 금지. 숫자는 시세표 값만 — 코드 백스톱(_unsupported_numbers)이 검증하고
#       위반 시 1회 재생성, 그래도 남으면 numbers_ok=False 로 저장(페이지는 표의 원 데이터를 항상 함께 보인다).
# 텔레그램: BRIEFING_TG_CHANNEL(예: @noil_market)이 env에 있으면 TELEGRAM_FILEBOT_TOKEN 봇으로 채널에 제목+리드+링크 게시.
# 수동: python 시장브리핑.py [--dry] [--force]
import os
import re
import sys
import json
import logging
from datetime import datetime, timedelta, date as _date

import requests
from pytz import timezone

import llm  # 구독 Claude(claude -p) → 실패 시 OpenAI 폴백
import indexnow
from redis_client import redis_client

log = logging.getLogger(__name__)
SEOUL = timezone("Asia/Seoul")
HASH = "market_briefings"
LATEST = "market_briefings:latest"
SITE = "https://hyeongeonnoil.com"
STALE_DAYS = 5  # 마지막 종가 날짜가 이보다 오래됐으면 표에서 제외(커피처럼 수집이 멈춘 시리즈)
WEEKDAY_KO = "월화수목금토일"

# (카테고리, 이름, 표시명, 단위, 소수 자릿수) — chart_data 해시의 실제 키 기준(2026-09-25 확인)
SYMBOLS = [
    ("currency", "usd_krw", "달러/원 환율", "원", 1),
    ("currency", "dxy", "달러인덱스", "", 2),
    ("currency", "usd_jpy", "달러/엔 환율", "엔", 2),
    ("treasury", "us-t10", "미국 10년물 금리", "%", 3),
    ("treasury", "kr-t3", "한국 3년물 금리", "%", 3),
    ("index", "kospi200", "코스피200", "", 2),
    ("index", "nasdaq100", "나스닥100", "", 2),
    ("index", "nikkei225", "닛케이225", "", 2),
    ("index", "hangseng", "항셍지수", "", 2),
    ("index", "dax", "독일 DAX", "", 2),
    ("commodity", "crude_oil", "WTI 원유", "달러", 2),
    ("commodity", "gold", "금", "달러", 2),
    ("commodity", "natural_gas", "천연가스", "달러", 3),
]
SECTIONS = [
    ("fx_rates", "환율·금리"),
    ("equities", "증시"),
    ("commodities_crypto", "원자재·코인"),
    ("news_link", "뉴스와 시장"),
    ("watch_today", "오늘 볼 것"),
]
# 영문 섹션 제목(고정 매핑 — LLM이 만들지 않음)
SECTION_HEADINGS_EN = {
    "fx_rates": "FX & Rates",
    "equities": "Equities",
    "commodities_crypto": "Commodities & Crypto",
    "news_link": "News and Markets",
    "watch_today": "What to Watch Today",
}
# 뉴스 분류 영문 고정 매핑(전일브리핑 enum 기준; 목록 밖 분류는 LLM 번역을 그대로 쓴다)
NEWS_CATEGORY_EN = {
    "경제·시장": "Economy & Markets",
    "지정학": "Geopolitics",
    "정치": "Politics",
    "사회·기타": "Society",
    "산업·기술": "Tech & Industry",
    "기술·산업": "Tech & Industry",
}
# 시세표 키 → 영문 표기(번역 프롬프트의 영문 시세표용) + 단위
LABEL_EN = {
    "usd_krw": ("USD/KRW", "won per dollar"),
    "dxy": ("Dollar Index", ""),
    "usd_jpy": ("USD/JPY", "yen per dollar"),
    "us-t10": ("US 10-year Treasury yield", "%"),
    "kr-t3": ("Korea 3-year yield", "%"),
    "kospi200": ("KOSPI 200", ""),
    "nasdaq100": ("Nasdaq 100", ""),
    "nikkei225": ("Nikkei 225", ""),
    "hangseng": ("Hang Seng", ""),
    "dax": ("DAX", ""),
    "crude_oil": ("WTI crude", "dollars per barrel"),
    "gold": ("gold", "dollars per ounce"),
    "natural_gas": ("natural gas", "dollars"),
    "btcusdt": ("Bitcoin", "dollars"),
}
# 사이트 시세 페이지 slug(/market/<slug>, /en/market/<slug>) — IndexNow 핑 대상
MARKET_SLUGS = ["usd-krw", "jpy-krw", "usd-jpy", "dxy", "us-10y", "kr-3y", "kospi200", "nasdaq100",
                "nikkei225", "hangseng", "dax", "gold", "crude-oil", "natural-gas"]
MONTH_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

SYSTEM = (
    "너는 한국 개인투자자를 위한 '아침 시장 브리핑' 작성자다. 입력은 ① 어제 종가 기준 시세표 ② 어제 세계 핵심 뉴스 5개 "
    "③ 한국·미국 뉴스 요약이다.\n"
    "규칙:\n"
    "1. 숫자는 시세표에 있는 값만 쓴다(반올림은 허용). 표에 없는 수치, 예상 수치, 과거 수치를 만들지 마라. "
    "숫자는 '1,389.2'처럼 아라비아 숫자와 쉼표로만 쓰고 '만·억' 단위 표기는 쓰지 마라.\n"
    "2. 전망·추천 금지: '오를 것이다', '매수 기회' 같은 문장 대신 '주목할 변수', '확인할 지표'로 쓴다. "
    "이 글은 정보 정리이고 투자 권유가 아니다.\n"
    "3. 제목(title)은 검색용이다: 어제 의미 있게 움직인 지표 1~2개의 방향과 숫자 + '{M월 D일} 시장 브리핑'. 38자 이내. "
    "달러/원 환율과 미국 10년물 금리는 검색 수요가 가장 크므로 둘 중 하나가 움직였으면 제목에 우선 넣는다. "
    "예: '달러 환율 1,389원 상승·미 10년물 4.12%: 9월 25일 시장 브리핑'. 금리는 소수 둘째 자리까지(5.11%), 환율은 원 단위 정수(1,369원)로 쓴다.\n"
    "4. lead는 2~3문장: 어제 시장의 한 줄 요약과 오늘 볼 것 하나.\n"
    "5. sections는 정확히 5개, key 순서 fx_rates(환율·금리) → equities(증시) → commodities_crypto(원자재·코인) → "
    "news_link(뉴스와 시장: 핵심 뉴스가 어느 자산에 어떻게 연결되는지, 확실한 인과만) → "
    "watch_today(오늘 볼 것: 입력에 근거가 있는 일정·변수만, 없으면 '확인할 지표' 위주). "
    "각 3~6문장, 한국어 평서문, 이모지·머리기호 금지.\n"
    "6. keywords는 검색어 형태 5~8개(예: '달러 환율', '미국 10년물 금리', '코스피200', '나스닥100', 'WTI 원유').\n"
    "7. movers는 등락률 절대값이 큰 순으로 3~5개(label은 시세표 표시명 그대로, direction은 up/down/flat, pct는 표의 등락률).\n"
    "반드시 한국어. 시세표에 없는 자산은 언급하지 마라."
)

SYSTEM_EN = (
    "You translate a Korean morning market briefing for Korean retail investors into natural English for an international "
    "audience. Input: (1) the Korean briefing as JSON (title, lead, sections, keywords), (2) the same market table in English "
    "with the exact figures.\n"
    "Rules:\n"
    "1. Translate faithfully. Keep every number and every direction (up/down/flat, higher/lower) exactly as in the Korean text. "
    "Never change, add, estimate or re-round a number; never introduce a figure that is not in the market table. "
    "Write numbers with Arabic digits and thousands separators (1,338.2). Do not use Korean units like 만/억.\n"
    "2. Asset names, always: USD/KRW, USD/JPY, Dollar Index, US 10-year Treasury yield (US 10Y in the title is fine), "
    "Korea 3-year yield, KOSPI 200, Nasdaq 100, Nikkei 225, Hang Seng, DAX, WTI crude, gold, natural gas, Bitcoin. "
    "Currency units: won, yen, dollars. Write the won rate as '1,338.2 won per dollar' (or 'USD/KRW 1,338.2'); "
    "the yen rate as '152.3 yen per dollar'. Rate moves in bp stay in bp.\n"
    "3. title is for search: the 1-2 indicators that moved, their direction and figure, then ': {Mon D} Korea market briefing'. "
    "Max 70 characters. Example: 'USD/KRW 1,338 lower, US 10Y 5.28%: Oct 8 Korea market briefing'. "
    "Keep the same indicators the Korean title chose; integer won for USD/KRW, two decimals for yields.\n"
    "4. No forecasts or recommendations, same as the original: use 'variables to watch', 'indicators to check' — never "
    "'will rise', 'buying opportunity'. This is information, not investment advice.\n"
    "5. sections: exactly the same keys as the input, same order, one translated body each (plain prose, no emoji, no bullets). "
    "Do not merge, drop or add sections.\n"
    "6. keywords: 5-8 English search phrases matching the Korean ones (e.g. 'USD/KRW exchange rate', 'US 10-year Treasury yield', "
    "'KOSPI 200', 'Nasdaq 100', 'WTI crude oil price').\n"
    "7. news: translate each item of the [Yesterday's top world news] list, same order and same count, one output item per input "
    "item. title: the headline in English. category: the English category label given in the input. summary: 1-2 sentences, "
    "at most 60 words, condensing the Korean bullet points (figures in the news may be kept as written; they are not bound by the "
    "market table). No bullets, no emoji, no commentary of your own.\n"
    "Output English only. In title, lead and sections do not mention assets that are not in the market table."
)

# 경량 경로(backfill: en 은 있고 news 만 없는 날) — 뉴스 5개만 번역
SYSTEM_NEWS_EN = (
    "You translate yesterday's top world news items (Korean headline, category, bullet-point summary) into natural English "
    "for an international audience.\n"
    "Rules: one output item per input item, same order, same count. title: the headline in English. category: the English "
    "category label given in the input. summary: 1-2 sentences, at most 60 words, condensing the Korean bullets; keep figures "
    "as written; no bullets, no emoji, no commentary. If an item has no summary, output an empty string for summary. "
    "Output English only."
)


def _schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "lead": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string", "enum": [k for k, _ in SECTIONS]},
                        "body": {"type": "string"},
                    },
                    "required": ["key", "body"],
                },
            },
            "keywords": {"type": "array", "items": {"type": "string"}},
            "movers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "direction": {"type": "string", "enum": ["up", "down", "flat"]},
                        "pct": {"type": "number"},
                    },
                    "required": ["label", "direction", "pct"],
                },
            },
        },
        "required": ["title", "lead", "sections", "keywords", "movers"],
    }


def _news_schema() -> dict:
    """en.news 배열 스키마 — 본문 번역 스키마와 경량(news만) 스키마가 공유."""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "category": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["title", "category", "summary"],
        },
    }


def _schema_en(with_news: bool = True) -> dict:
    props = {
        "title": {"type": "string"},
        "lead": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string", "enum": [k for k, _ in SECTIONS]},
                    "body": {"type": "string"},
                },
                "required": ["key", "body"],
            },
        },
        "keywords": {"type": "array", "items": {"type": "string"}},
    }
    required = ["title", "lead", "sections", "keywords"]
    if with_news:
        props["news"] = _news_schema()
        required.append("news")
    return {"type": "object", "additionalProperties": False, "properties": props, "required": required}


def _schema_news_en() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"news": _news_schema()},
        "required": ["news"],
    }


# ── 시세 스냅샷 ───────────────────────────────────────────────────────────────
def _load_cat(cat: str) -> dict:
    raw = redis_client.hget("chart_data", cat)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return {}


def _btc_row(today: _date) -> dict | None:
    """Bybit 일봉은 UTC 기준(09:00 KST 시작). 07시 실행 시 list[0]은 진행 중 캔들 → 현재가, list[1] 종가 = 전일 09시 기준."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": "BTCUSDT", "interval": "D", "limit": 3},
            timeout=8,
        )
        lst = ((r.json().get("result") or {}).get("list")) or []
        if len(lst) < 2:
            return None
        cur, prev = float(lst[0][4]), float(lst[1][4])
        chg = cur - prev
        return {
            "key": "btcusdt", "label": "비트코인(BTC/USDT)", "unit": "달러", "dec": 0,
            "date": today.isoformat(), "prev_date": (today - timedelta(days=1)).isoformat(),
            "close": round(cur), "prev_close": round(prev), "chg": round(chg), "pct": round(chg / prev * 100, 2) if prev else 0.0,
            "ma100": None, "vs_ma_pct": None, "note": "오전 7시 현재가 기준, 전일은 09시 일봉 종가",
        }
    except Exception as e:
        log.warning("⚠️ BTC 시세 조회 실패: %s", e)
        return None


def market_snapshot(today: _date) -> list[dict]:
    out, cache = [], {}
    today_s = today.isoformat()
    for cat, name, label, unit, dec in SYMBOLS:
        d = cache.setdefault(cat, _load_cat(cat))
        # 오늘 날짜 행은 진행 중 캔들(매시 갱신)이라 제외 → 실행 시각과 무관하게 "어제 종가"가 된다.
        rows = [r for r in (((d.get(name) or {}).get("data")) or [])
                if r.get("close") is not None and r.get("date") and str(r["date"]) < today_s]
        if len(rows) < 2:
            continue
        last, prev = rows[-1], rows[-2]
        try:
            last_date = datetime.strptime(last["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - last_date).days > STALE_DAYS:
            log.info("⏭️ %s 마지막 종가 %s — 오래돼 제외", name, last["date"])
            continue
        close, pclose = float(last["close"]), float(prev["close"])
        chg = close - pclose
        ma = last.get("ma100")
        row = {
            "key": name, "label": label, "unit": unit, "dec": dec,
            "date": last["date"], "prev_date": prev["date"],
            "close": round(close, dec), "prev_close": round(pclose, dec),
            "chg": round(chg, dec), "pct": round(chg / pclose * 100, 2) if pclose else 0.0,
            "ma100": round(float(ma), dec) if ma is not None else None,
            "vs_ma_pct": round((close / float(ma) - 1) * 100, 2) if ma else None,
        }
        if unit == "%":
            row["chg_bp"] = round(chg * 100, 1)  # 금리는 bp로도
        out.append(row)
    btc = _btc_row(today)
    if btc:
        out.append(btc)
    return out


def _market_table(snapshot: list[dict]) -> str:
    lines = []
    for s in snapshot:
        dec, u = s["dec"], s["unit"]
        if u == "%":
            line = f"- {s['label']}: {s['close']:.3f}% ({s['date']} 종가, 전일 {s['prev_close']:.3f}% → {s['chg_bp']:+.1f}bp)"
            if s.get("ma100") is not None:
                line += f" | 100일 평균 {s['ma100']:.3f}%"
        else:
            line = (f"- {s['label']}: {s['close']:,.{dec}f}{u} ({s['date']} 종가, 전일 {s['prev_close']:,.{dec}f} → "
                    f"{s['chg']:+,.{dec}f}, {s['pct']:+.2f}%)")
            if s.get("ma100") is not None:
                line += f" | 100일 평균 {s['ma100']:,.{dec}f}({s['vs_ma_pct']:+.2f}%)"
        if s.get("note"):
            line += f" | {s['note']}"
        lines.append(line)
    return "\n".join(lines)


# ── 뉴스 입력 ─────────────────────────────────────────────────────────────────
def _news_items(news_day: str) -> list:
    """전일브리핑 items[5](rank·title·summary·category) — Redis 히스토리 키(TTL 30일). 없으면 []."""
    raw = redis_client.get(f"news:daily_briefing:{str(news_day or '').replace('-', '')}")
    if not raw:
        return []
    try:
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw).get("items", []) or []
    except Exception:
        return []


def _news_input(news_day: str) -> tuple[list, str, str]:
    items = _news_items(news_day)
    ko = us = ""
    try:
        from 전일브리핑 import _collect_day_summaries
        per_country = _collect_day_summaries(news_day)
        ko = (per_country.get("Korea") or "")[:1800]
        us = (per_country.get("USA") or "")[:1800]
    except Exception as e:
        log.warning("⚠️ 국가별 요약 조회 실패(뉴스 5개만 사용): %s", e)
    return items, ko, us


# ── 숫자 백스톱 ───────────────────────────────────────────────────────────────
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
SMALL_OK = {100.0, 200.0, 225.0, 50.0, 30.0, 2025.0, 2026.0}  # 지수 이름·100일평균·연도


def _allowed_values(snapshot: list[dict]) -> list[float]:
    vals = []
    for s in snapshot:
        for k in ("close", "prev_close", "chg", "pct", "ma100", "vs_ma_pct", "chg_bp"):
            v = s.get(k)
            if v is not None:
                vals.append(abs(float(v)))
    return vals


def _unsupported_numbers(text: str, allowed: list[float]) -> list[str]:
    """본문의 숫자 중 시세표 값의 반올림으로 설명되지 않는 것. 31 이하 정수(날짜·개수)와 지수 이름 숫자는 허용."""
    bad = []
    for m in NUM_RE.finditer(text):
        tok = m.group(0).replace(",", "")
        try:
            f = float(tok)
        except ValueError:
            continue
        dec = len(tok.split(".")[1]) if "." in tok else 0
        if (dec == 0 and f <= 31) or f in SMALL_OK:
            continue
        tol = 0.5 * 10 ** (-dec) + 1e-9
        if any(abs(f - a) <= tol for a in allowed):
            continue
        bad.append(m.group(0))
    return sorted(set(bad))


def _all_text(data: dict) -> str:
    return "\n".join([data.get("title", ""), data.get("lead", "")] + [s.get("body", "") for s in data.get("sections", [])])


# ── 생성 ──────────────────────────────────────────────────────────────────────
def generate_market_briefing(today: _date, news_day: str, snapshot: list[dict] | None = None) -> dict:
    snapshot = snapshot if snapshot is not None else market_snapshot(today)
    if len(snapshot) < 5:
        raise RuntimeError(f"시세 입력 부족 ({len(snapshot)}종)")
    items, ko, us = _news_input(news_day)
    label = f"{today.month}월 {today.day}일({WEEKDAY_KO[today.weekday()]})"
    news_lines = [f"{i.get('rank', '')}. [{i.get('category', '')}] {i.get('title', '')} — {' / '.join(s.lstrip('• ').strip() for s in str(i.get('summary', '')).splitlines() if s.strip())}" for i in items]
    user = (
        f"오늘 날짜: {today.isoformat()} {label} 오전 7시 (KST)\n\n"
        f"[시세표 — 어제 종가 기준]\n{_market_table(snapshot)}\n\n"
        f"[어제 세계 핵심 뉴스 5개 ({news_day})]\n" + ("\n".join(news_lines) or "(없음)") +
        f"\n\n[한국 뉴스 요약]\n{ko or '(없음)'}\n\n[미국 뉴스 요약]\n{us or '(없음)'}"
    )
    allowed = _allowed_values(snapshot)
    log.info("📈 시장 브리핑 입력: day=%s 시세 %d종 뉴스 %d건 news_day=%s", today, len(snapshot), len(items), news_day)
    data = llm.structured("market_briefing", user, _schema(), system=SYSTEM)
    bad = _unsupported_numbers(_all_text(data), allowed)
    if bad:
        log.warning("⚠️ 시세표에 없는 숫자 %s → 1회 재생성", bad)
        fix = f"\n\n[수정 요청] 직전 초안에 시세표에 없는 숫자가 있었다: {', '.join(bad)}. 시세표의 숫자만 사용해 다시 작성하라."
        data = llm.structured("market_briefing", user + fix, _schema(), system=SYSTEM)
        bad = _unsupported_numbers(_all_text(data), allowed)
        if bad:
            log.warning("⚠️ 재생성 후에도 남은 숫자 %s — numbers_ok=False 로 저장", bad)
    by_key = {s.get("key"): s.get("body", "") for s in data.get("sections", [])}
    sections = [{"key": k, "heading": h, "body": (by_key.get(k) or "").strip()} for k, h in SECTIONS]
    data_date = max((s["date"] for s in snapshot if s["key"] != "btcusdt"), default=today.isoformat())
    return {
        "day": today.isoformat(),
        "generated_at": datetime.now(SEOUL).isoformat(),
        "data_date": data_date,
        "news_day": news_day,
        "title": (data.get("title") or "").strip(),
        "lead": (data.get("lead") or "").strip(),
        "sections": sections,
        "keywords": [k.strip() for k in data.get("keywords", []) if k and k.strip()][:8],
        "movers": data.get("movers", [])[:5],
        "snapshot": snapshot,
        "news_items": [{"title": i.get("title", ""), "category": i.get("category", "")} for i in items],
        "numbers_ok": not bad,
        "unsupported_numbers": bad,
    }


# ── 영문 번역(2026-10-09) ─────────────────────────────────────────────────────
def _market_table_en(snapshot: list[dict]) -> str:
    """번역 모델에 주는 영문 시세표 — 숫자는 한국어 표와 동일, 이름·단위만 영문."""
    lines = []
    for s in snapshot:
        name, unit = LABEL_EN.get(s["key"], (s.get("label", s["key"]), ""))
        dec = s["dec"]
        if s.get("unit") == "%":
            line = f"- {name}: {s['close']:.3f}% (close {s['date']}, prev {s['prev_close']:.3f}% → {s.get('chg_bp', 0):+.1f}bp)"
            if s.get("ma100") is not None:
                line += f" | 100-day avg {s['ma100']:.3f}%"
        else:
            u = f" {unit}" if unit else ""
            line = (f"- {name}: {s['close']:,.{dec}f}{u} (close {s['date']}, prev {s['prev_close']:,.{dec}f} → "
                    f"{s['chg']:+,.{dec}f}, {s['pct']:+.2f}%)")
            if s.get("ma100") is not None:
                line += f" | 100-day avg {s['ma100']:,.{dec}f}({s['vs_ma_pct']:+.2f}%)"
        if s["key"] == "btcusdt":
            line += " | spot at 07:00 KST; prev = previous daily close (09:00 KST)"
        lines.append(line)
    return "\n".join(lines)


def _flatten_summary(summary) -> str:
    """전일브리핑 summary(줄바꿈 불릿) → ' / ' 로 이은 한 줄(생성 프롬프트와 같은 규칙)."""
    return " / ".join(s.lstrip("• ").strip() for s in str(summary or "").splitlines() if s.strip())


def _news_source(b: dict, items: list | None = None) -> list[dict]:
    """번역 원본 뉴스 [{title, category, category_en, summary}].
    1순위 전일브리핑 items(summary 포함, Redis TTL 30일) → 없으면 b['news_items'](title·category만, summary='')."""
    src = items if items is not None else _news_items(b.get("news_day") or "")
    if not src:
        src = b.get("news_items") or []
    out = []
    for i in src:
        if not isinstance(i, dict):
            continue
        title = str(i.get("title") or "").strip()
        if not title:
            continue
        cat = str(i.get("category") or "").strip()
        out.append({
            "title": title,
            "category": cat,
            "category_en": NEWS_CATEGORY_EN.get(cat, ""),
            "summary": _flatten_summary(i.get("summary")),
        })
    return out[:5]


def _news_block_en(src: list[dict]) -> str:
    """프롬프트의 뉴스 입력 블록. 분류는 고정 매핑이 있으면 영문 라벨을 함께 준다."""
    if not src:
        return "(none)"
    lines = []
    for n, s in enumerate(src, 1):
        cat = s["category_en"] or s["category"]
        hint = "" if s["category_en"] else " (translate this category into English)"
        lines.append(f"{n}. category: {cat}{hint}\n   title: {s['title']}\n   summary: {s['summary'] or '(none)'}")
    return "\n".join(lines)


def _merge_news_en(data_news, src: list[dict]) -> list[dict]:
    """LLM news 배열 + 원본 → 저장 형식 [{title, category, summary}]. 개수는 원본 기준, 빠진 항목은 원문 제목·빈 요약.
    분류는 고정 매핑 우선(LLM 이 바꿔 적어도 무시), 매핑 밖이면 LLM 번역 → 그것도 없으면 원문."""
    got = data_news if isinstance(data_news, list) else []
    out = []
    for i, s in enumerate(src):
        g = got[i] if i < len(got) and isinstance(got[i], dict) else {}
        title = str(g.get("title") or "").strip() or s["title"]
        cat = s["category_en"] or str(g.get("category") or "").strip() or s["category"]
        summary = str(g.get("summary") or "").strip() if s["summary"] else ""  # 원문 요약 없으면 요약 생성 금지
        out.append({"title": title, "category": cat, "summary": summary})
    return out


def _en_user_prompt(b: dict, news_src: list[dict] | None = None) -> str:
    """번역 입력: 영문 날짜 라벨 + 한국어 본문 JSON + 영문 시세표 + (news_src 가 있으면) 어제 세계 핵심 뉴스."""
    try:
        d = datetime.strptime(b["day"], "%Y-%m-%d").date()
        date_label = f"{MONTH_EN[d.month - 1]} {d.day}"
    except Exception:
        date_label = b.get("day", "")
    ko = {
        "title": b.get("title", ""),
        "lead": b.get("lead", ""),
        "sections": [{"key": s.get("key"), "body": s.get("body", "")} for s in b.get("sections", [])],
        "keywords": b.get("keywords", []),
    }
    out = (
        f"Briefing date: {b.get('day', '')} (title date label: '{date_label}'), 07:00 KST\n\n"
        f"[Korean briefing JSON]\n{json.dumps(ko, ensure_ascii=False, indent=1)}\n\n"
        f"[Market table in English — same figures as the Korean one]\n{_market_table_en(b.get('snapshot') or [])}"
    )
    if news_src is not None:
        out += (f"\n\n[Yesterday's top world news ({b.get('news_day', '')}) — translate each, same order, {len(news_src)} items]\n"
                + _news_block_en(news_src))
    return out


def translate_briefing_en(b: dict, items: list | None = None) -> dict:
    """한국어 브리핑 dict → 영문 {title, lead, sections[{key,heading,body}], keywords, news[{title,category,summary}],
    numbers_ok, generated_at}. LLM 1회(본문+뉴스 한 스키마). 실패 시 예외(호출 측에서 삼킴).
    숫자 백스톱(_unsupported_numbers)은 title/lead/sections 에만(_all_text) — news 는 시세표 밖 숫자가 당연하므로 제외.
    items: 전일브리핑 items(summary 포함)를 이미 들고 있으면 전달; None 이면 Redis 에서 news_day 로 다시 읽는다."""
    news_src = _news_source(b, items)
    user = _en_user_prompt(b, news_src)
    allowed = _allowed_values(b.get("snapshot") or [])
    data = llm.structured("market_briefing_en", user, _schema_en(), system=SYSTEM_EN)
    bad = _unsupported_numbers(_all_text(data), allowed) if allowed else []
    if bad:
        log.warning("⚠️ 영문 번역에 시세표에 없는 숫자 %s → 1회 재시도", bad)
        fix = (f"\n\n[Correction] The previous draft contained figures not in the market table in title/lead/sections: "
               f"{', '.join(bad)}. Translate again using only the figures that appear in the Korean text and the market table "
               f"(the news items are exempt from this rule).")
        data = llm.structured("market_briefing_en", user + fix, _schema_en(), system=SYSTEM_EN)
        bad = _unsupported_numbers(_all_text(data), allowed) if allowed else []
        if bad:
            log.warning("⚠️ 영문 재시도 후에도 남은 숫자 %s — 그대로 저장(numbers_ok=False)", bad)
    by_key = {s.get("key"): s.get("body", "") for s in data.get("sections", [])}
    sections = [{"key": k, "heading": SECTION_HEADINGS_EN[k], "body": (by_key.get(k) or "").strip()} for k, _ in SECTIONS]
    title = (data.get("title") or "").strip()
    lead = (data.get("lead") or "").strip()
    if not title or not lead or not any(s["body"] for s in sections):
        raise RuntimeError("영문 번역 결과가 비어 있음")
    news = _merge_news_en(data.get("news"), news_src)
    if news_src and len(data.get("news") or []) != len(news_src):
        log.warning("⚠️ 영문 뉴스 개수 불일치(원본 %d, 번역 %d) — 빠진 항목은 원문 제목으로 채움", len(news_src), len(data.get("news") or []))
    return {
        "title": title,
        "lead": lead,
        "sections": sections,
        "keywords": [k.strip() for k in data.get("keywords", []) if isinstance(k, str) and k.strip()][:8],
        "news": news,
        "numbers_ok": not bad,
        "generated_at": datetime.now(SEOUL).isoformat(),
    }


def translate_news_en(b: dict, items: list | None = None) -> list[dict]:
    """경량 경로(백필): 뉴스 5개만 영문 번역 → [{title, category, summary}]. 본문(title/lead/sections)은 건드리지 않는다.
    원본은 _news_items(news_day)(Redis TTL 30일) → 없으면 b['news_items'](title·category만 → summary='').
    원본 자체가 없으면 [] 반환(LLM 호출 없음). 실패 시 예외(호출 측에서 처리)."""
    news_src = _news_source(b, items)
    if not news_src:
        return []
    user = (f"Briefing date: {b.get('day', '')} — yesterday's top world news ({b.get('news_day', '')}), "
            f"{len(news_src)} items. Translate each, same order.\n\n" + _news_block_en(news_src))
    data = llm.structured("market_briefing_en", user, _schema_news_en(), system=SYSTEM_NEWS_EN)
    got = data.get("news") or []
    if len(got) != len(news_src):
        log.warning("⚠️ 영문 뉴스 개수 불일치(원본 %d, 번역 %d) — 빠진 항목은 원문 제목으로 채움", len(news_src), len(got))
    return _merge_news_en(got, news_src)


def attach_en(b: dict, items: list | None = None) -> dict:
    """b['en'] 채우기(본문+news). 번역 실패는 경고 로그만 — 한국어 저장은 그대로 진행."""
    try:
        b["en"] = translate_briefing_en(b, items)
    except Exception as e:
        log.warning("⚠️ 시장 브리핑 영문 번역 실패(en 생략) day=%s: %s", b.get("day"), e)
        b.pop("en", None)
    return b


def briefing_urls(b: dict) -> list[str]:
    """IndexNow 핑 대상: 브리핑 2개 언어 + 목록 + 홈 + 시세 페이지 전부."""
    day = b["day"]
    urls = [f"/briefing/{day}"]
    if b.get("en"):
        urls.append(f"/en/briefing/{day}")
    urls += ["/briefing", "/"]
    urls += [f"/market/{s}" for s in MARKET_SLUGS] + [f"/en/market/{s}" for s in MARKET_SLUGS]
    return urls


# ── 저장·알림·진입점 ──────────────────────────────────────────────────────────
def _dec(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def latest_briefing() -> dict | None:
    day = _dec(redis_client.get(LATEST))
    if not day:
        return None
    raw = redis_client.hget(HASH, day)
    try:
        return json.loads(_dec(raw)) if raw else None
    except Exception:
        return None


def already_done(day: str) -> bool:
    return bool(redis_client.hexists(HASH, day))


def store_market_briefing(b: dict):
    redis_client.hset(HASH, b["day"], json.dumps(b, ensure_ascii=False))
    redis_client.set(LATEST, b["day"])


def _notify_telegram(b: dict):
    tok = (os.getenv("TELEGRAM_FILEBOT_TOKEN") or "").strip()
    chat = (os.getenv("BRIEFING_TG_CHANNEL") or "").strip()
    if not tok or not chat:
        return
    text = f"{b['title']}\n\n{b['lead']}\n\n{SITE}/briefing/{b['day']}"
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=10)
    except Exception as e:
        log.warning("⚠️ 브리핑 텔레그램 전송 실패: %s", e)


def generate_and_store_market_briefing(force: bool = False, dry: bool = False) -> dict | None:
    """스케줄러/수동 진입점. 같은 날 중복·시세 미갱신(주말)은 스킵. force=True로 강제."""
    now = datetime.now(SEOUL)
    today = now.date()
    day = today.isoformat()
    if not force and already_done(day):
        log.info("⏭️ 시장 브리핑 이미 생성됨 (day=%s) — 스킵", day)
        return None
    snapshot = market_snapshot(today)
    data_date = max((s["date"] for s in snapshot if s["key"] != "btcusdt"), default=None)
    prev = latest_briefing()
    if not force and prev and data_date and prev.get("data_date") == data_date:
        log.info("⏭️ 시세 갱신 없음(data_date=%s, 직전 브리핑 %s와 동일) — 스킵", data_date, prev.get("day"))
        return None
    from 전일브리핑 import _target_day
    b = generate_market_briefing(today, _target_day(now), snapshot)
    attach_en(b)  # 영문 번역(실패 시 en 생략)
    if dry:
        print(json.dumps(b, ensure_ascii=False, indent=1))
        return b
    store_market_briefing(b)
    _notify_telegram(b)
    log.info("📈 시장 브리핑 저장 day=%s numbers_ok=%s en=%s en.news=%d건 title=%s",
             b["day"], b["numbers_ok"], "en" in b, len((b.get("en") or {}).get("news") or []), b["title"])
    indexnow.ping(briefing_urls(b))
    return b


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = generate_and_store_market_briefing(force="--force" in sys.argv, dry="--dry" in sys.argv)
    if out is None:
        print("(스킵됨)")
    elif "--dry" not in sys.argv:
        print(f"저장됨: {SITE}/briefing/{out['day']} — {out['title']}")

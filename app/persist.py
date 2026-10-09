# persist.py
import os
import json
import logging
from datetime import datetime, time, date, timedelta

import requests
from pytz import timezone
from supabase import create_client

# redis_client = 로컬 트레이딩 Redis(트레이딩 read), news_redis = Upstash(뉴스 read / daily_equity write).
# TRADING_REDIS_URL 미설정 시 둘 다 Upstash라 기존과 동일 동작.
from redis_client import trading_redis_client as redis_client, redis_client as news_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SEOUL = timezone("Asia/Seoul")


def decode_val(v):
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", errors="ignore")

    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v

    return v


def decode_hash(h):
    out = {}
    for k, v in h.items():
        key = k.decode("utf-8", errors="ignore") if isinstance(k, (bytes, bytearray)) else str(k)
        out[key] = decode_val(v)
    return out


def normalize_reasons(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = [value]

    if isinstance(value, list):
        return value

    return []


def to_float_or_none(v, *, positive_only=True):
    if v is None:
        return None

    try:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            v = v.replace("%", "")

        f = float(v)

        if positive_only and f <= 0:
            return None

        return f
    except Exception:
        return None


def pick_trade_price_with_source(r, source_signal=None):
    """
    Supabase trade_records.price에 넣을 가격 선택.

    ENTRY:
      entry_price → price → source_signal.price

    EXIT:
      exit_price → price → source_signal.price

    주의:
      0.0은 유효 가격으로 보지 않음.
    """
    kind = str(r.get("kind") or r.get("action") or "").upper()
    source_signal = source_signal or {}

    if kind == "ENTRY":
        candidates = [
            ("trade_record.entry_price", r.get("entry_price")),
            ("trade_record.price", r.get("price")),
            ("source_signal.price", source_signal.get("price")),
        ]
    elif kind == "EXIT":
        candidates = [
            ("trade_record.exit_price", r.get("exit_price")),
            ("trade_record.price", r.get("price")),
            ("source_signal.price", source_signal.get("price")),
        ]
    else:
        candidates = [
            ("trade_record.price", r.get("price")),
            ("trade_record.exit_price", r.get("exit_price")),
            ("trade_record.entry_price", r.get("entry_price")),
            ("source_signal.price", source_signal.get("price")),
        ]

    for src, value in candidates:
        f = to_float_or_none(value)
        if f is not None:
            return f, src

    return None, None


def resolve_pnl_usdt_from_record(r, source_signal=None):
    """
    표준 PnL 키는 pnl_usdt만 사용.
    trade_record.pnl_usdt가 없으면 EXIT에 한해 source_signal.price로 USDT PnL 복구.
    """
    source_signal = source_signal or {}

    existing_pnl = to_float_or_none(r.get("pnl_usdt"), positive_only=False)
    existing_gross = to_float_or_none(r.get("gross_pnl_usdt"), positive_only=False)
    existing_fee = to_float_or_none(r.get("fee_usdt"), positive_only=False)

    if existing_pnl is not None:
        return existing_gross, existing_fee, existing_pnl, "trade_record.pnl_usdt"

    kind = str(r.get("kind") or r.get("action") or "").upper()
    if kind != "EXIT":
        return None, None, None, None

    side = str(r.get("side") or "").upper()
    qty = to_float_or_none(r.get("qty"))
    entry_price = to_float_or_none(r.get("entry_price"))

    if entry_price is None:
        entry_price = to_float_or_none(source_signal.get("entry_price"))

    exit_price, price_source = pick_trade_price_with_source(r, source_signal)

    fee_rate = to_float_or_none(r.get("fee_rate"), positive_only=False)
    if fee_rate is None:
        fee_rate = 0.00055

    if side not in ("LONG", "SHORT"):
        return None, None, None, None

    if qty is None or entry_price is None or exit_price is None:
        return None, None, None, None

    if side == "LONG":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty

    fee = (entry_price * qty + exit_price * qty) * fee_rate
    pnl = gross - fee

    return gross, fee, pnl, f"calculated_from_{price_source}"


def load_signals_by_signal_id(namespace="bybit", search_back=5000):
    """
    Redis 원본 signal stream에서 signal_id 기준으로 reasons_json을 찾기 위한 맵.
    trade_records ENTRY에 reasons_json이 빠졌을 때 보강용.
    """
    stream_key = f"trading:{namespace}:signals"
    out = {}

    try:
        rows = redis_client.xrevrange(
            stream_key,
            max="+",
            min="-",
            count=search_back,
        )
    except Exception as e:
        log.warning("⚠️ signals stream 읽기 실패 key=%s err=%s", stream_key, e)
        return out

    for msg_id, fields in rows:
        item = decode_hash(fields)

        sid = (
                item.get("signal_id")
                or item.get("id")
                or item.get("_id")
        )

        if not sid:
            continue

        sid = str(sid)

        if sid not in out:
            item["_stream_id"] = (
                msg_id.decode() if isinstance(msg_id, (bytes, bytearray)) else str(msg_id)
            )
            out[sid] = item

    log.info("✅ signals loaded by signal_id count=%d key=%s", len(out), stream_key)
    return out


def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SECRET_KEY 환경변수가 필요함")

    return create_client(url, key)


def extract_position_symbols(asset_data):
    if not isinstance(asset_data, dict):
        return []

    symbols = []

    for key, value in asset_data.items():
        if not str(key).startswith("positions."):
            continue

        symbol = str(key).replace("positions.", "").upper()
        pos = value if isinstance(value, dict) else {}

        has_position = False

        for side in ("LONG", "SHORT"):
            side_pos = pos.get(side)
            if side_pos and float(side_pos.get("qty") or 0) != 0:
                has_position = True
                break

        if has_position:
            symbols.append(symbol)

    return sorted(set(symbols))


def fetch_bybit_last_close(symbol, end_dt):
    """
    day_end 직전 마지막 1분봉 close 조회.
    end_dt는 timezone-aware KST datetime.
    """
    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = end_ms - 2 * 60 * 60 * 1000

    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "1",
        "start": str(start_ms),
        "end": str(end_ms),
        "limit": "120",
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()

    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit retCode={j.get('retCode')} retMsg={j.get('retMsg')}")

    rows = j.get("result", {}).get("list", []) or []
    parsed = []

    for row in rows:
        try:
            ts_ms = int(row[0])
            close = float(row[4])

            if ts_ms < end_ms:
                parsed.append((ts_ms, close))
        except Exception:
            continue

    if not parsed:
        return None

    parsed.sort(key=lambda x: x[0])
    return parsed[-1][1]


def fetch_close_prices_for_asset(asset_data, day_end):
    symbols = extract_position_symbols(asset_data)
    out = {}

    for symbol in symbols:
        try:
            close = fetch_bybit_last_close(symbol, day_end)

            if close is not None:
                out[symbol] = close
                log.info("✅ close price %s=%s", symbol, close)
            else:
                log.warning("⚠️ close price 없음: %s", symbol)

        except Exception as e:
            log.warning("⚠️ close price 조회 실패 %s: %s", symbol, e)

    return out


def calc_asset_equity(asset_data, close_prices):
    if not isinstance(asset_data, dict):
        return None, None

    wallet = asset_data.get("wallet.USDT")

    try:
        wallet = float(wallet)
    except Exception:
        wallet = None

    if wallet is None:
        return None, None

    unrealized = 0.0

    for key, value in asset_data.items():
        if not str(key).startswith("positions."):
            continue

        symbol = str(key).replace("positions.", "").upper()
        close = close_prices.get(symbol)

        if close is None:
            continue

        pos = value if isinstance(value, dict) else {}

        for side in ("LONG", "SHORT"):
            side_pos = pos.get(side)

            if not side_pos:
                continue

            entries = side_pos.get("entries") or []

            for e in entries:
                try:
                    qty = float(e.get("qty") or 0)
                    entry = float(e.get("price") or 0)
                except Exception:
                    continue

                if side == "LONG":
                    unrealized += (close - entry) * qty
                else:
                    unrealized += (entry - close) * qty

    return wallet + unrealized, unrealized


def load_lots_by_entry_signal_id():
    pattern = "trading:agent:CopyZannavi:u7c9f14d2a1:BYBIT:lot:*"
    out = {}

    try:
        for key in redis_client.scan_iter(match=pattern, count=500):
            if redis_client.type(key) != b"hash":
                continue

            lot = decode_hash(redis_client.hgetall(key))
            entry_signal_id = lot.get("entry_signal_id")

            if not entry_signal_id:
                continue

            out[str(entry_signal_id)] = lot

    except Exception as e:
        log.warning("⚠️ lot keys 읽기 실패: %s", e)

    log.info("✅ lots loaded by entry_signal_id count=%d", len(out))
    return out


def get_latest_thresholds_by_symbol(symbols, namespace="bybit", search_back=500):
    """
    Redis Stream trading:{namespace}:OpenPctLog 에서
    심볼별 가장 최근 new 값을 ma_threshold로 가져옴.
    coin 쪽 /api/thresholds.ts 와 같은 기준.
    """
    wanted = {str(s).upper() for s in symbols if s}

    if not wanted:
        return {}

    stream_key = f"trading:{namespace}:OpenPctLog"
    found = {}

    try:
        rows = redis_client.xrevrange(
            stream_key,
            max="+",
            min="-",
            count=search_back,
        )
    except Exception as e:
        log.warning("⚠️ OpenPctLog 읽기 실패 key=%s err=%s", stream_key, e)
        return {}

    for msg_id, fields in rows:
        item = decode_hash(fields)

        sym = str(
            item.get("sym")
            or item.get("SYM")
            or item.get("symbol")
            or ""
        ).upper()

        if not sym or sym not in wanted or sym in found:
            continue

        raw_new = item.get("new") or item.get("NEW")

        try:
            ma_threshold = float(raw_new)
        except Exception:
            continue

        source_id = msg_id.decode() if isinstance(msg_id, (bytes, bytearray)) else str(msg_id)

        found[sym] = {
            "ma_threshold": ma_threshold,
            "momentum_threshold": ma_threshold / 3,
            "source_id": source_id,
            "raw": item,
        }

        if len(found) >= len(wanted):
            break

    log.info(
        "✅ OpenPctLog thresholds loaded key=%s wanted=%d found=%d",
        stream_key,
        len(wanted),
        len(found),
    )

    return found


def persist_today_data(
        dry_run=False,
        target_day=None,
        include_current_day=False,
        save_asset_snapshot=True,
):
    """
    Supabase에 하루치 데이터를 저장.

    기준 window:
      day_start = 해당 날짜 06:50 KST
      day_end   = 다음 날짜 06:50 KST

    target_day:
      특정 날짜 강제 저장.
      예: target_day="2026-05-14"
      → 2026-05-14 06:50 ~ 2026-05-15 06:50

    include_current_day=False:
      운영 스케줄용.
      항상 직전 완료된 day 저장.
      예: 5/14 06:55 실행
      → 5/13 06:50 ~ 5/14 06:50 저장

    include_current_day=True:
      서버 시작/수동 테스트용.
      현재 진행 중인 day 저장.
      예: 5/14 낮 실행
      → 5/14 06:50 ~ 5/15 06:50 window 기준 현재까지 저장
    """
    supabase = None if dry_run else get_supabase()

    now = datetime.now(SEOUL)
    boundary = time(6, 50)

    if target_day is not None:
        if isinstance(target_day, str):
            target_date = datetime.strptime(target_day, "%Y-%m-%d").date()
        elif isinstance(target_day, date):
            target_date = target_day
        else:
            raise ValueError("target_day는 'YYYY-MM-DD' 문자열 또는 date 객체여야 함")

        day_start = SEOUL.localize(datetime.combine(target_date, boundary))
        day_end = day_start + timedelta(days=1)

    else:
        today_0650 = SEOUL.localize(datetime.combine(now.date(), boundary))

        if include_current_day:
            # 서버 시작/수동 실행용: 현재 진행 중인 day
            if now >= today_0650:
                day_start = today_0650
                day_end = today_0650 + timedelta(days=1)
            else:
                day_start = today_0650 - timedelta(days=1)
                day_end = today_0650
        else:
            # 운영 06:55 스케줄용: 직전 완료된 day
            if now >= today_0650:
                day_start = today_0650 - timedelta(days=1)
                day_end = today_0650
            else:
                day_start = today_0650 - timedelta(days=2)
                day_end = today_0650 - timedelta(days=1)

    day = day_start.strftime("%Y-%m-%d")

    # ✅ target_day 재처리는 과거 asset 덮어쓰기 위험이 있으므로 기본 차단
    if target_day is not None and save_asset_snapshot:
        log.warning(
            "⚠️ target_day=%s 재처리에서 현재 Redis asset으로 asset_snapshots를 덮을 위험이 있어 "
            "asset snapshot 저장을 자동 스킵합니다.",
            target_day,
        )
        save_asset_snapshot = False


    def is_today_trade_record(r):
        ts_ms = r.get("ts_ms") or r.get("timestamp_ms") or r.get("saved_ts_ms")

        if not ts_ms:
            return False

        try:
            ts_ms = int(float(ts_ms))
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=SEOUL)

            # 중복 방지 위해 day_end는 미포함
            return day_start <= dt < day_end

        except Exception:
            return False

    log.info(
        "📦 Supabase persist 시작 day=%s window=%s~%s dry_run=%s include_current_day=%s target_day=%s",
        day,
        day_start,
        day_end,
        dry_run,
        include_current_day,
        target_day,
    )

    # 1) 뉴스/유튜브 등 daily 수집 데이터
    target_day_key = day_start.strftime("%Y%m%d")
    news_key = f"news:daily_saved_data:{target_day_key}"

    news_raw = news_redis.get(news_key)  # 뉴스는 Upstash
    news_data = decode_val(news_raw) if news_raw else None

    log.info("news_data exists=%s key=%s", bool(news_data), news_key)

    # 2) 매매 실행 기록 trade_records — 계좌별(BYBIT + MT5) 수집
    #    (2026-08-20) MT5 계좌 추가: Redis 스트림 보존이 10일이라 여기서 영구 보존하지 않으면 소실됨.
    #    id_prefix: 두 스트림의 entry id(ms-seq)가 충돌할 수 있어 MT5만 프리픽스(기존 BYBIT row id 호환 유지).
    TRADE_ACCOUNTS = [
        {
            "account": "BYBIT",
            "key": "trading:agent:CopyZannavi:u7c9f14d2a1:BYBIT:trade_records",
            "id_prefix": "",
            "signal_ns": ["s11", "s22", "bybit", "cryptod", "s1", "s2"],
        },
        {
            "account": "MT5",
            "key": "trading:agent:CopyZannaviMT5:u8f3a9c1e7b:MT5:trade_records",
            "id_prefix": "mt5-",
            "signal_ns": ["s11m", "s22m", "mt5", "fxd", "mt5d"],
        },
        {
            # (2026-10-05) 환율 전용 MT5 계좌(FXmode 235290369, executor-a3). account 는 종전처럼 "MT5" —
            # 사이트·앱·보고서가 MT5 를 한 묶음으로 읽고 환율/비환율은 심볼로 가른다. 신호 ns 는 위 MT5 와 같아 다시 안 읽음.
            "account": "MT5",
            "key": "trading:agent:CopyZannaviFX:u5d2b7e4c9a:MT5:trade_records",
            "id_prefix": "mt5fx-",
            "signal_ns": [],
        },
    ]

    trade_records = []

    for acc in TRADE_ACCOUNTS:
        try:
            trade_records_raw = redis_client.xrevrange(
                acc["key"],
                max="+",
                min="-",
                count=5000,
            )
        except Exception as e:
            log.warning("⚠️ trade_records 읽기 실패 key=%s err=%s", acc["key"], e)
            trade_records_raw = []

        # ✅ 정정값 오버레이 (2026-09-18): MT5 청산 pnl_usdt 가 (가격차×랏수)로 기록돼 계약크기·손익통화 환산이
        #    빠졌던 결함의 백필값(tradingBot/tools/backfill_mt5_pnl.py)이 `<key>:override` 해시(field=entry id,
        #    value=JSON)에 있으면 스트림 원본을 덮어쓴다. 없으면 종전 그대로. (/api/tradeRecords 도 동일 규칙)
        overrides = {}
        try:
            for k, v in (redis_client.hgetall(f"{acc['key']}:override") or {}).items():
                k = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                v = v.decode() if isinstance(v, (bytes, bytearray)) else v
                ov = json.loads(v) if isinstance(v, str) else v
                if isinstance(ov, dict):
                    overrides[k] = ov
        except Exception as e:
            log.warning("⚠️ trade_records override 읽기 실패 key=%s err=%s", acc["key"], e)

        for msg_id, fields in trade_records_raw:
            item = decode_hash(fields)

            item["_id"] = (
                msg_id.decode() if isinstance(msg_id, (bytes, bytearray)) else str(msg_id)
            )
            ov = overrides.get(item["_id"])
            if ov:
                item.update(ov)
            item["_account"] = acc["account"]
            item["_id_prefix"] = acc["id_prefix"]

            trade_records.append(item)

    before_count = len(trade_records)
    trade_records = [r for r in trade_records if is_today_trade_record(r)]

    log.info(
        "trade_records(BYBIT+MT5) before_count=%d day_count=%d day=%s",
        before_count,
        len(trade_records),
        day,
    )

    # thresholds 조회용 심볼은 기존 동작 유지(Bybit 계좌만 — OpenPctLog가 Bybit 페이지 데이터)
    signal_symbols = sorted({
        str(r.get("symbol", "")).upper()
        for r in trade_records
        if isinstance(r, dict) and r.get("symbol")
        and str(r.get("_account") or "BYBIT") == "BYBIT"
    })

    # ENTRY에 reasons_json이 없는 경우 원본 signals stream에서 보강 —
    # (2026-08-20) bybit 단일 → 전 채널 병합 (s11/s22 거래의 전략 태그가 소실되던 문제 수정)
    signals_by_id = {}
    for _ns in [n for a in TRADE_ACCOUNTS for n in a["signal_ns"]]:
        _m = load_signals_by_signal_id(namespace=_ns, search_back=5000)
        for _sid, _sig in _m.items():
            signals_by_id.setdefault(_sid, _sig)

    # 3) 현재 자산
    asset_key_candidates = [
        "trading:agent:CopyZannavi:u7c9f14d2a1:BYBIT:asset",
    ]

    asset_data = None
    used_asset_key = None

    for key in asset_key_candidates:
        try:
            if redis_client.type(key) == b"hash":
                asset_data = decode_hash(redis_client.hgetall(key))
                used_asset_key = key
                break
        except Exception as e:
            log.warning("⚠️ asset key 확인 실패 key=%s err=%s", key, e)

    log.info("asset key=%s exists=%s", used_asset_key, bool(asset_data))

    if dry_run:
        log.info("✅ dry-run 완료 day=%s", day)
        log.info("news sample=%s", news_data)
        log.info("trade_records sample=%s", trade_records[:2])
        log.info("asset sample=%s", asset_data)
        return {
            "day": day,
            "day_start": day_start.isoformat(),
            "day_end": day_end.isoformat(),
            "news_exists": bool(news_data),
            "trade_records_count": len(trade_records),
            "asset_exists": bool(asset_data),
        }

    # 1) daily 저장 — youtube transcript는 별도 테이블, raw_json에서 제거
    news_data_stripped = news_data
    if isinstance(news_data, dict) and isinstance(news_data.get("youtube_data"), dict):
        youtube_data_stripped = {}
        transcript_rows = []
        for country, info in news_data["youtube_data"].items():
            if not isinstance(info, dict):
                youtube_data_stripped[country] = info
                continue
            sc = info.get("summary_content")
            youtube_data_stripped[country] = {k: v for k, v in info.items() if k != "summary_content"}
            if sc:
                transcript_rows.append({"day": day, "country": country, "summary_content": sc})
        news_data_stripped = {**news_data, "youtube_data": youtube_data_stripped}

        if transcript_rows:
            supabase.table("youtube_transcripts").upsert(
                transcript_rows, on_conflict="day,country"
            ).execute()
            log.info("✅ youtube_transcripts 저장 완료 day=%s count=%d", day, len(transcript_rows))

    # 뉴스 데이터가 없으면(예: 과거일 재persist) daily_collections를 건드리지 않는다.
    # (news_data=None으로 upsert하면 raw_json=null이 되어 기존 뉴스 백필이 지워짐)
    if news_data is not None:
        supabase.table("daily_collections").upsert({
            "day": day,
            "raw_json": news_data_stripped,
            "updated_at": now.isoformat(),
        }).execute()
        log.info("✅ daily_collections 저장 완료 day=%s", day)
        # 아카이브 페이지 즉시 색인 요청(IndexNow, 2026-10-09). 실패는 indexnow 안에서 로그만.
        from indexnow import ping as _indexnow_ping
        _indexnow_ping([f"/archive/{day}", "/archive"])
    else:
        log.info("⏭️ news_data 없음 → daily_collections 건너뜀(기존 백필 보존) day=%s", day)

    # 2) trade_records 저장
    trade_rows = []

    for idx, r in enumerate(trade_records):
        if not isinstance(r, dict):
            r = {"raw": r}

        signal_id = (
                r.get("signal_id")
                or r.get("exit_signal_id")
                or r.get("entry_signal_id")
                or r.get("_id")
        )

        raw_json = dict(r)

        # ✅ 계좌 구분: 내부 필드는 빼고 raw_json.account로 노출, MT5는 row id 프리픽스
        account = str(raw_json.pop("_account", "") or "BYBIT")
        id_prefix = str(raw_json.pop("_id_prefix", "") or "")
        raw_json["account"] = account

        row_id = id_prefix + str(r.get("_id") or signal_id or f"{day}-{idx}")

        # 1) trade_record 자체 reasons 우선
        reasons = normalize_reasons(raw_json.get("reasons_json"))

        # 2) trade_record에 reasons_json이 없으면 원본 signal에서 보강
        signal_ref_ids = [
            raw_json.get("signal_id"),
            raw_json.get("entry_signal_id"),
            raw_json.get("open_signal_id"),
            raw_json.get("exit_signal_id"),
            raw_json.get("anchor_open_signal_id"),
        ]

        source_signal = None

        for sid in signal_ref_ids:
            if not sid:
                continue

            source_signal = signals_by_id.get(str(sid))

            if source_signal:
                break

        if source_signal:
            source_reasons = normalize_reasons(
                source_signal.get("reasons_json")
                or source_signal.get("reasons")
            )

            if not reasons and source_reasons:
                reasons = source_reasons
                raw_json["reasons_json"] = reasons

            # 원본 signal도 raw_json에 남겨두면 디버깅 쉬움
            raw_json["source_signal"] = source_signal

        # 3) 최종 표시용 signal 결정
        signal_kind = (
                raw_json.get("signal")
                or raw_json.get("signal_kind")
                or raw_json.get("reason")
                or raw_json.get("signal_type")
                or raw_json.get("entry_reason")
                or raw_json.get("exit_reason")
                or (reasons[0] if reasons else None)
                # ✅ (2026-08-20) executor가 lot에 박제한 전략 태그 — 신호 스트림(35일) 만료 후에도 귀속 유지
                or raw_json.get("strategy_tag")
                or raw_json.get("kind")
        )

        raw_json["signal"] = signal_kind
        raw_json["signal_kind"] = signal_kind
        raw_json["display_kind"] = signal_kind
        raw_json["display_label"] = f"{signal_kind} {raw_json.get('side') or ''}".strip()

        trade_price, trade_price_source = pick_trade_price_with_source(raw_json, source_signal)

        gross_pnl_usdt, fee_usdt, pnl_usdt, pnl_source = resolve_pnl_usdt_from_record(
            raw_json,
            source_signal,
        )

        raw_json["resolved_price"] = trade_price
        raw_json["resolved_price_source"] = trade_price_source

        if gross_pnl_usdt is not None:
            raw_json["gross_pnl_usdt_resolved"] = gross_pnl_usdt

        if fee_usdt is not None:
            raw_json["fee_usdt_resolved"] = fee_usdt

        if pnl_usdt is not None:
            raw_json["pnl_usdt_resolved"] = pnl_usdt
            raw_json["pnl_resolved_from"] = pnl_source

        trade_rows.append({
            "id": row_id,
            "day": day,
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "kind": r.get("kind"),

            "signal": signal_kind,
            "display_label": raw_json.get("display_label"),

            # ✅ ENTRY면 entry price, EXIT면 exit price, 없으면 source_signal.price
            "price": trade_price,

            "qty": r.get("qty"),

            # ✅ 기존 pnl_usdt 없으면 source_signal.price로 계산한 USDT PnL
            "pnl": pnl_usdt,

            "raw_json": raw_json,
        })

    if trade_rows:
        supabase.table("trade_records").upsert(
            trade_rows,
            on_conflict="id",
        ).execute()
        log.info("✅ trade_records 저장 완료 day=%s count=%d", day, len(trade_rows))
    else:
        log.info("⏭️ trade_records 저장할 데이터 없음 day=%s", day)

    # 3) asset snapshot 저장
    if not save_asset_snapshot:
        log.info("⏭️ asset snapshot 저장 스킵 day=%s save_asset_snapshot=False", day)

    elif asset_data:
        if not isinstance(asset_data, dict):
            asset_data = {"raw": asset_data}

        wallet = asset_data.get("wallet.USDT")

        # 과거 날짜 재저장일 경우 해당 day_end 기준 종가 사용
        # 현재 진행 중인 day면 now 기준
        price_ref_time = min(now, day_end)

        close_prices = fetch_close_prices_for_asset(asset_data, price_ref_time)

        # MA100 envelope 기준값 저장
        asset_symbols = extract_position_symbols(asset_data)
        symbols_for_thresholds = sorted(set(signal_symbols + asset_symbols))

        thresholds = get_latest_thresholds_by_symbol(
            symbols_for_thresholds,
            namespace="bybit",
            search_back=500,
        )

        # raw_json 안에도 저장해서 프론트에서 바로 사용 가능하게 함
        asset_data["close_prices"] = close_prices
        asset_data["close_price_at"] = price_ref_time.isoformat()
        asset_data["thresholds"] = thresholds
        asset_data["thresholds_source"] = {
            "key": "trading:bybit:OpenPctLog",
            "saved_at": now.isoformat(),
        }

        equity_usdt, unrealized_pnl_usdt = calc_asset_equity(asset_data, close_prices)

        asset_data["equity_usdt"] = equity_usdt
        asset_data["unrealized_pnl_usdt"] = unrealized_pnl_usdt

        supabase.table("asset_snapshots").upsert({
            "day": day,
            "equity_usdt": equity_usdt if equity_usdt is not None else wallet,
            "wallet_usdt": wallet,
            "raw_json": asset_data,
        }, on_conflict="day").execute()

        log.info(
            "✅ asset_snapshots 저장 완료 day=%s equity=%s unrealized=%s close_symbols=%d",
            day,
            equity_usdt,
            unrealized_pnl_usdt,
            len(close_prices),
        )
    else:
        log.info("⏭️ asset snapshot 저장할 데이터 없음 day=%s", day)

    # 4) MT5(데모) 일일 equity 스냅샷 — CFD 페이지 자산추이용. Redis 해시에 영구 기록.
    #    equity = wallet.USD + Σ positions.*.pnl (executor-a2가 발행하는 브로커 정확값)
    if save_asset_snapshot:
        try:
            save_mt5_daily_equity(day, now)
        except Exception as e:
            log.warning("⚠️ MT5 daily equity 저장 실패 day=%s: %s", day, e)
        try:
            save_mt5_daily_equity(day, now, MT5FX_ASSET_KEY, MT5FX_DAILY_EQUITY_KEY, "MT5-환율")
        except Exception as e:
            log.warning("⚠️ MT5 환율 계좌 daily equity 저장 실패 day=%s: %s", day, e)

    log.info("🎉 Supabase persist 완료 day=%s", day)

    return {
        "day": day,
        "day_start": day_start.isoformat(),
        "day_end": day_end.isoformat(),
        "news_exists": bool(news_data),
        "trade_records_count": len(trade_records),
        "asset_exists": bool(asset_data),
    }


MT5_ASSET_KEY = "trading:agent:CopyZannaviMT5:u8f3a9c1e7b:MT5:asset"
MT5_DAILY_EQUITY_KEY = "trading:agent:CopyZannaviMT5:u8f3a9c1e7b:MT5:daily_equity"
# 환율 전용 MT5 계좌(2026-10-05~, executor-a3)
MT5FX_ASSET_KEY = "trading:agent:CopyZannaviFX:u5d2b7e4c9a:MT5:asset"
MT5FX_DAILY_EQUITY_KEY = "trading:agent:CopyZannaviFX:u5d2b7e4c9a:MT5:daily_equity"


def save_mt5_daily_equity(day: str, now, asset_key: str = MT5_ASSET_KEY, out_key: str = MT5_DAILY_EQUITY_KEY, label: str = "MT5"):
    """MT5(데모) 계정 일일 equity 스냅샷 → Redis 해시(day → json). 프론트 /api/mt5-equity가 읽음.
    equity = wallet.USD + Σ positions.*.{LONG,SHORT}.pnl (봇 발행 정확 손익)."""
    if redis_client.type(asset_key) != b"hash":
        log.info("⏭️ %s asset 해시 없음 → daily equity 스킵", label)
        return

    asset = decode_hash(redis_client.hgetall(asset_key))

    wallet = None
    try:
        wallet = float(asset.get("wallet.USD"))
    except (TypeError, ValueError):
        pass
    if wallet is None:
        log.info("⏭️ MT5 wallet.USD 없음 → daily equity 스킵")
        return

    unrealized = 0.0
    for key, value in asset.items():
        if not str(key).startswith("positions."):
            continue
        pos = value
        if isinstance(pos, str):
            try:
                pos = json.loads(pos)
            except Exception:
                continue
        if not isinstance(pos, dict):
            continue
        for side in ("LONG", "SHORT"):
            p = pos.get(side)
            if isinstance(p, dict) and p.get("pnl") is not None:
                try:
                    unrealized += float(p["pnl"])
                except (TypeError, ValueError):
                    pass

    equity = wallet + unrealized
    payload = json.dumps({
        "equity_usd": round(equity, 2),
        "wallet_usd": round(wallet, 2),
        "unrealized_usd": round(unrealized, 2),
        "saved_at": now.isoformat(),
    }, ensure_ascii=False)

    news_redis.hset(out_key, day, payload)  # 프론트(mt5-equity.ts)가 Upstash서 읽으므로 Upstash에 기록
    log.info("✅ %s daily equity 저장 day=%s equity=%.2f (wallet=%.2f, unrl=%.2f)",
             label, day, equity, wallet, unrealized)


def persist_recent_days(
        days=5,
        dry_run=False,
        include_current_day=True,
        save_asset_snapshot=False,
):
    """
    최근 N일치 Supabase 업데이트.
    day 기준은 06:50 ~ 다음날 06:50.

    include_current_day=True:
      현재 진행 중인 day 포함해서 최근 N일.
      예: 5/14 낮 실행 → 5/14, 5/13, 5/12 ...

    include_current_day=False:
      완료된 day만 최근 N일.
      예: 5/14 낮 실행 → 5/13, 5/12, 5/11 ...
    """
    now = datetime.now(SEOUL)
    boundary = time(6, 50)
    today_0650 = SEOUL.localize(datetime.combine(now.date(), boundary))

    if now >= today_0650:
        current_day_start = today_0650
    else:
        current_day_start = today_0650 - timedelta(days=1)

    if not include_current_day:
        current_day_start = current_day_start - timedelta(days=1)

    log.info(
        "📦 recent persist 시작 days=%d dry_run=%s include_current_day=%s",
        days,
        dry_run,
        include_current_day,
    )

    for i in range(days):
        target_date = (current_day_start - timedelta(days=i)).date()

        log.info("====== 최근 %d/%d day=%s 업데이트 시작 ======", i + 1, days, target_date)

        try:
            persist_today_data(
                dry_run=dry_run,
                target_day=target_date,
                include_current_day=False,
                save_asset_snapshot=save_asset_snapshot,
            )
        except Exception as e:
            log.exception("❌ day=%s 업데이트 실패: %s", target_date, e)

    log.info("🎉 recent persist 완료 days=%d", days)


if __name__ == "__main__":
    # False = 실제 Supabase 저장
    # True = 저장 안 하고 로그만 확인
    DRY_RUN = False

    # 수동 실행/테스트:
    # 현재 진행 중인 day 포함해서 최근 2일 저장
    persist_recent_days(
        days=9,
        dry_run=DRY_RUN,
        include_current_day=True,
    )

    # 하루치만 저장하고 싶으면 위 recent 호출을 주석 처리하고 아래 사용
    # 운영 스케줄과 같은 동작: 직전 완료 day 저장
    # persist_today_data(dry_run=DRY_RUN, include_current_day=False)

    # 서버 시작 시와 같은 동작: 현재 진행 중인 day 저장
    # persist_today_data(dry_run=DRY_RUN, include_current_day=True)

    # 특정 날짜 강제 저장
    # persist_today_data(dry_run=DRY_RUN, target_day="2026-05-14")

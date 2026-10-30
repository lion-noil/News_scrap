import time
from datetime import datetime, UTC
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
import requests
import os
import json
from datetime import datetime, timedelta
# 환경변수 불러오기
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
CACHE_PATH = Path(__file__).resolve().parent / "token_cache.json"


def calculate_moving_average(data, period=100):
    result = []
    for i in range(len(data)):
        if i < period:
            pass
        else:
            avg = sum(d["close"] for d in data[i - period + 1:i + 1]) / period
            result.append(avg)
    return result

def _dev_sigma(data, moving_avg):
    """이탈(종가/이평-1)의 표준편차 — 밴드를 자산별 σ 배수로 그리기 위한 값. moving_avg 는 data 뒤쪽에 정렬돼 있음."""
    if not moving_avg:
        return None
    closes = [d["close"] for d in data[-len(moving_avg):]]
    devs = [c / m - 1 for c, m in zip(closes, moving_avg) if m]
    if len(devs) < 30:
        return None
    mean = sum(devs) / len(devs)
    return (sum((x - mean) ** 2 for x in devs) / len(devs)) ** 0.5

def calculate_envelope(moving_avg, percentage):
    upper = []
    lower = []
    for avg in moving_avg:
        upper.append(avg * (1 + percentage))
        lower.append(avg * (1 - percentage))
    return upper, lower

def load_cached_token():
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r") as f:
            cache = json.load(f)
            expires_at = datetime.fromisoformat(cache["expires_at"])
            if datetime.now() < expires_at:
                return cache["access_token"]
    return None

def save_token_to_cache(access_token, expires_at_str):
    # 문자열 → datetime 객체
    expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S")
    with open(CACHE_PATH, "w") as f:
        json.dump({
            "access_token": access_token,
            "expires_at": expires_at.isoformat()
        }, f)

def get_access_token(KIS_APP_KEY,KIS_APP_SECRET ):
    cached_token = load_cached_token()
    if cached_token:
        print("✅ Using cached access token.")
        return cached_token

    print("🔐 Requesting new access token...")
    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }

    response = requests.post(url, headers=headers, json=body)
    response.raise_for_status()
    data = response.json()
    access_token = data["access_token"]
    expires_at_str = data.get("access_token_token_expired")
    if not expires_at_str:
        # 만료 시간 정보가 없을 경우 24시간 유효
        expires_at_str = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    save_token_to_cache(access_token, expires_at_str)
    return access_token

def process_row_data(row, field_map, volume_key="acml_vol", extra_fields=None):
    """공통적인 row 처리 함수"""
    return {
        "date": datetime.strptime(row['stck_bsop_date'], "%Y%m%d").strftime("%Y-%m-%d"),
        "open": round(float(row[field_map["open"]]), 6),
        "high": round(float(row[field_map["high"]]), 6),
        "low": round(float(row[field_map["low"]]), 6),
        "close": round(float(row[field_map["close"]]), 6),
        "volume": safe_int(row.get(volume_key, 0)),
        **(extra_fields or {})
    }
def fetch_stock_or_index_prices(symbol,token,category="index", source="domestic", num_days=400):
    # (2026-10-30) 250일 이동평균·이탈 σ 계산용으로 2년치(≈500거래일)까지 받는다. 저장은 여전히 최근 100일.
    time.sleep(0.5)
    today = datetime.today()
    start_date = (today - timedelta(days=730))  # 2년 전
    end_date = today# 오늘

    if source == "domestic":
        # 국내 주식/지수의 경우
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        tr_id = "FHKST03010100"
        market_code = "J"

        current_end = end_date
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P"
        }
        all_data = []

        while True:
            current_start = current_end - timedelta(days=120)  # 넉넉히 120일 간격 (휴장일 고려)
            if current_start < start_date:
                current_start = start_date

            params = {
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": current_start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                "FID_ORG_ADJ_PRC": "0",
                "FID_PERIOD_DIV_CODE": "D"
            }
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            # JSON 데이터 반환
            data = response.json()
            if 'output2' not in data or not data['output2']:
                break  # 더 이상 데이터가 없으면 종료

            all_data.extend(data['output2'])

            # 가장 오래된 날짜에서 하루 전으로 다음 루프 설정
            oldest = min(data['output2'], key=lambda x: x['stck_bsop_date'])
            current_end = datetime.strptime(oldest['stck_bsop_date'], "%Y%m%d") - timedelta(days=1)

            if current_end < start_date:
                break
        data = {'output2':all_data}

    elif source == "dmr":
        # 국내 주식/지수의 경우
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
        tr_id = "FHKUP03500100"

        current_end = end_date
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P"
        }
        all_data = []
        market_code = 'U'
        while True:
            current_start = current_end - timedelta(days=120)  # 넉넉히 120일 간격 (휴장일 고려)
            if current_start < start_date:
                current_start = start_date

            params = {
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": current_start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D"
            }
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            # JSON 데이터 반환
            data = response.json()
            if 'output2' not in data or not data['output2']:
                break  # 더 이상 데이터가 없으면 종료

            all_data.extend(data['output2'])

            # 가장 오래된 날짜에서 하루 전으로 다음 루프 설정
            oldest = min(data['output2'], key=lambda x: x['stck_bsop_date'])
            current_end = datetime.strptime(oldest['stck_bsop_date'], "%Y%m%d") - timedelta(days=1)

            if current_end < start_date:
                break
        data = {'output2': all_data}

    elif source == "overseas":
        url = "https://openapi.koreainvestment.com:9443/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
        tr_id = "FHKST03030100"
        if category=="index":
            market_code = "N"
        elif category=="currency":
            market_code = "X"
        elif category=="treasury":
            market_code = "I"
        elif category == "commodity":
            market_code = "N"
        else:
            market_code = "N"
        current_end = datetime.today()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P"
        }
        all_data = []
        while True:
            current_start = current_end - timedelta(days=120)  # 넉넉히 120일 간격 (휴장일 고려)
            if current_start < start_date:
                current_start = start_date

            params = {
                "FID_COND_MRKT_DIV_CODE": market_code,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": current_start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": current_end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D"
            }
        # API 호출
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
        # JSON 데이터 반환
            data = response.json()
            if 'output2' not in data or not data['output2']:
                break  # 더 이상 데이터가 없으면 종료
            all_data.extend(data['output2'])

            # 가장 오래된 날짜에서 하루 전으로 다음 루프 설정
            oldest = min(data['output2'], key=lambda x: x['stck_bsop_date'])
            current_end = datetime.strptime(oldest['stck_bsop_date'], "%Y%m%d") - timedelta(days=1)

            if current_end < start_date:
                break
        data = {'output2': all_data}
    elif source == "osFutures":
        url = "https://openapi.koreainvestment.com:9443/uapi/overseas-futureoption/v1/quotations/daily-ccnl"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": "HHDFC55020100",
            "custtype": "P"  # 개인고객
        }
        all_data = []
        qry_tp = "Q"  # 최초 조회
        index_key = ""
        exchange = "CME"
        symbol = 'MGCM25'
        while True:
            params = {
                "SRS_CD": symbol,  # 종목코드 예: "6AM24"
                "EXCH_CD": exchange,  # 거래소 예: "CME"
                "START_DATE_TIME": start_date.strftime("%Y%m%d"),
                "CLOSE_DATE_TIME": end_date.strftime("%Y%m%d"),
                "QRY_TP": qry_tp,
                "QRY_CNT": "40",
                "QRY_GAP": "",
                "INDEX_KEY": index_key
            }

            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            output2 = data.get("output2", [])
            if not output2:
                break

            all_data.extend(output2)

            # 다음 조회를 위해 트랜잭션 헤더에서 확인 필요 (tr_cont, index_key)
            tr_cont = data.get("output1", {}).get("tr_cont", "")
            if tr_cont != "M":  # M이면 다음 데이터 있음
                break

            # output1 또는 별도 키에서 다음 조회용 INDEX_KEY 가져와야 하는데 문서에 명확히 없음
            # 일반적으로 output1 내부에 다음 조회용 키가 있음 (가이드 문서 참고)
            index_key = data.get("output1", {}).get("index_key", "")
            if not index_key:
                break

            qry_tp = "P"  # 다음 조회

        return all_data

    else:
        raise ValueError("Invalid source type. Use 'domestic' or 'overseas'.")


    if 'output2' in data and isinstance(data['output2'], list):
        # 데이터를 최신 날짜 기준으로 정렬한 후, 최근 100일 치 데이터만 추출
        sorted_data = sorted(data['output2'], key=lambda x: x['stck_bsop_date'])
        data_filtered = sorted_data[-num_days:]
    else:
        data_filtered = data.get('output2', [])  # 오류나 데이터가 없을 경우 기본 빈 리스트

    result = []
    for i, row in enumerate(data_filtered):
        if source =="domestic":
            result.append(process_row_data(row, {
                "open": "stck_oprc",
                "high": "stck_hgpr",
                "low": "stck_lwpr",
                "close": "stck_clpr",
            }))

        elif source == "dmr":
            result.append(process_row_data(row, {
                "open": "bstp_nmix_oprc",
                "high": "bstp_nmix_hgpr",
                "low": "bstp_nmix_lwpr",
                "close": "bstp_nmix_prpr",
            }))

        elif source =="overseas":
            open_ = round(float(row['ovrs_nmix_oprc']), 6)
            high = round(float(row['ovrs_nmix_hgpr']), 6)
            low = round(float(row['ovrs_nmix_lwpr']), 6)
            close = round(float(row['ovrs_nmix_prpr']), 6)

            # 환율 반전 처리
            if symbol in {"FX@EUR", "FX@GBP"} :
                open_, high, low, close = (
                    round(1 / open_, 6),
                    round(1 / low, 6),  # low ↔ high
                    round(1 / high, 6),
                    round(1 / close, 6),
                )

            result.append({
                "date": datetime.strptime(row['stck_bsop_date'], "%Y%m%d").strftime("%Y-%m-%d"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": safe_int(row.get("acml_vol", 0))
            })
    return result
def safe_int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
def fetch_stock_info(symbol, token, category,source="krx", day_num=200, ma_period=100):
    processed_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:

        data = fetch_stock_or_index_prices(symbol, token, category=category, source=source)
    except ValueError as e:
        print(f"Error: {e}")
        data = None  # 또는 적절한 기본값

    moving_avg = calculate_moving_average(data, period=ma_period)
    upper10, lower10 = calculate_envelope(moving_avg, 0.10)
    upper3, lower3 = calculate_envelope(moving_avg, 0.03)
    # (2026-10-30) 250일 이평 + 이탈 σ — 자산군별 대표값(원자재 250일)과 ±kσ 밴드용. 과거가 모자라면 None.
    ma250 = calculate_moving_average(data, period=250)
    sig100 = _dev_sigma(data, moving_avg)
    sig250 = _dev_sigma(data, ma250)

    trimmed_data = data[-len(moving_avg):]
    off250 = len(trimmed_data) - len(ma250)
    for i in range(len(trimmed_data)):
        trimmed_data[i]["ma100"] = moving_avg[i]
        trimmed_data[i]["envelope10_upper"] = upper10[i]
        trimmed_data[i]["envelope10_lower"] = lower10[i]
        trimmed_data[i]["envelope3_upper"] = upper3[i]
        trimmed_data[i]["envelope3_lower"] = lower3[i]
        trimmed_data[i]["ma250"] = ma250[i - off250] if i >= off250 else None
    trimmed_data = trimmed_data[-100:]  # 저장은 최근 100일
    return {'processed_time': processed_time, 'data': trimmed_data,
            'stats': {'sig100': sig100, 'sig250': sig250, 'hist_days': len(data)}}

# 각 지표들의 평균 계산 (있는 경우에만)

def calculate_dxy_from_currency_data(token, ma_period=100) -> list:
    processed_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    required = ["usd_eur", "usd_jpy", "usd_gbp", "usd_cad", "usd_sek", "usd_chf"]
    os_CURRENCY_SYMBOLS = {
        "usd_jpy": "FX@JPY",  # 일본 엔
        "usd_eur": "FX@EUR",  # 유로 유로->달러임
        "usd_gbp": "FX@GBP",  # 영국 파운드 파운드->달러
        "usd_cad": "FX@CAD",  # 캐나다 달러
        "usd_sek": "FX@SEK",  # 스웨덴 크로나
        "usd_chf": "FX@CHF",  # 스위스 프랑
    }

    weights = {
        'usd_eur': 0.576,
        'usd_jpy': 0.136,
        'usd_gbp': 0.119,
        'usd_cad': 0.091,
        'usd_sek': 0.042,
        'usd_chf': 0.036
    }
    # DXY 구성 통화의 환율 데이터 불러오기
    currency_data = {}
    for ticker in required:
        symbol = os_CURRENCY_SYMBOLS[ticker]
        raw = fetch_stock_or_index_prices(symbol, token, category='currency', source='overseas')
        df = pd.DataFrame(raw)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        df = df[['date', 'close']]
        currency_data[symbol] = df.set_index('date')['close']
    # 날짜 기준 병합
    merged_df = pd.concat(currency_data.values(), axis=1)
    merged_df.columns = required
    merged_df = merged_df.dropna()
    # DXY 계산

    dxy = 50.14348112 * (
            merged_df['usd_eur'] ** weights['usd_eur'] *
            merged_df['usd_jpy'] ** weights['usd_jpy'] *
            merged_df['usd_gbp'] ** weights['usd_gbp'] *
            merged_df['usd_cad'] ** weights['usd_cad'] *
            merged_df['usd_sek'] ** weights['usd_sek'] *
            merged_df['usd_chf'] ** weights['usd_chf']
    )

    dxy_df = pd.DataFrame({
        'close': dxy
    })
    dxy_df['ma100'] = dxy_df['close'].rolling(window=100).mean()
    dxy_df['ma250'] = dxy_df['close'].rolling(window=250).mean()
    _d100 = (dxy_df['close'] / dxy_df['ma100'] - 1).dropna()
    _d250 = (dxy_df['close'] / dxy_df['ma250'] - 1).dropna()
    _stats = {'sig100': float(_d100.std(ddof=0)) if len(_d100) >= 30 else None,
              'sig250': float(_d250.std(ddof=0)) if len(_d250) >= 30 else None, 'hist_days': int(len(dxy_df))}
    # 100일 이동 평균이 이미 있다면
    dxy_df['envelope10_upper'] = dxy_df['ma100'] * 1.10
    dxy_df['envelope10_lower'] = dxy_df['ma100'] * 0.90
    dxy_df['envelope3_upper'] = dxy_df['ma100'] * 1.03
    dxy_df['envelope3_lower'] = dxy_df['ma100'] * 0.97
    recent_df = dxy_df.sort_index().tail(100)[[
        'close', 'ma100', 'ma250', 'envelope10_upper', 'envelope10_lower', 'envelope3_upper', 'envelope3_lower'
    ]].reset_index()

    recent_df['date'] = recent_df['date'].dt.strftime('%Y-%m-%d')

    # 열 순서 정리
    recent_df = recent_df[
        ['date', 'close', 'ma100', 'ma250', 'envelope10_upper', 'envelope10_lower', 'envelope3_upper', 'envelope3_lower']]
    recent_df = recent_df.astype(object).where(pd.notnull(recent_df), None)  # NaN(ma250 미충족) → null

    # 리스트[dict] 형태로 변환
    result = recent_df.to_dict(orient='records')

    return {'processed_time': processed_time, 'data': result, 'stats': _stats}


# ✅ 테스트 실행
if __name__ == "__main__":
    from app.test_config import ALL_SYMBOLS
    token = get_access_token(KIS_APP_KEY, KIS_APP_SECRET)

    results = []
    calculate_dxy_from_currency_data(token)
    for source, symbol_dict in ALL_SYMBOLS.items():
        for category, symbols in symbol_dict.items():
            source_data = {}
            for name, symbol in symbols.items():
                try:
                    # fetch_stock_info 호출 시, symbol과 source 전달
                    new_data = fetch_stock_info(symbol, token, category,source=source, day_num=200)
                    source_data[name] = new_data
                    results.append(f"✅ [{source.upper()} - {category.upper()} - {name.upper()}] {len(new_data['data'])}개 데이터 수집 완료")
                except Exception as e:
                    results.append(f"❌ [{source.upper()} - {category.upper()} - {name.upper()}] 수집 중 오류 발생: {str(e)}")
    print(results)
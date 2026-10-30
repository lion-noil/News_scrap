channels = [
        {"country": "Korea",
         "channel_handle": "@newskbs",
         "playlist_id": ["PL9a4x_yPK_85sGRvAQX4LEVHY8F9v405J","PL9a4x_yPK_84yhk67LjymVv7kbI5eyOVU"],
         "keyword": ["[풀영상] 뉴스12", "[풀영상] 뉴스광장"],
         "save_fields": "subtitle"},
        {
            "country": "USA",
            "channel_handle": "@PBSNewsHour",
            "playlist_id": "PLgawtcOBBjr9w4YGiJrPD8f6MXTCIUfK9",
            "keyword": ["PBS News Hour full episode", "PBS News Weekend full episode"],
            "save_fields": "subtitle"
        },
        {
            "country": "Japan",
            "channel_handle": "@tbsnewsdig",
            "keyword": "（Japan News Digest Live）",
            "playlist_id": "PLhoNlZaJqDLaPgn1NqC9FxMPnlkemRpyr",
            "save_fields": "subtitle"
        },
        {
            "country": "China",
            "channel_handle": "@CCTV",
            "playlist_id": "PL0eGJygpmOH5xQuy8fpaOvKrenoCsWrKh",
            "keyword": "CCTV「新闻联播」",
            "save_fields": "description"
        },
        {
            "country": "Germany",
            "channel_handle": "@tagesschau",
            "playlist_id": "PLkKDSXRppVa7FFdxr-wQaNltbtvOkZvx_",
            "keyword": " / ",
            "save_fields": "description"
        },
        {
            "country": "UK",
            "channel_handle": "@BBCNews",
            "playlist_id": "PLS3XGZxi7cBU_AtRadnVPJPRC6LHrexaF",
            "keyword": "BBC Newscast",
            "save_fields": "subtitle"
        },
        {
            "country": "India",
            "channel_handle": "@DDnews",
            "playlist_id": "",
            "keyword": "LIVE: News Night",
            "save_fields": "subtitle"
        },
        {
            "country": "HongKong",
            "channel_handle": "@tvbnewsofficial",
            "playlist_id": "PLKoXXVQa3yxBlMRa-1lq0gDur_RstqgLr",
            "keyword": "TVB News at 7:30",
            "save_fields": "subtitle"
        },
        # (2026-10-27 추가) 호주 — 9 News 시드니 저녁 풀 뉴스(16~18분, 주말 포함 매일 ~08:45 UTC).
        # 'Full News Bulletins' 재생목록이 최신순 + 5개 도시판이 같이 올라와 제목 정규식으로 시드니판만.
        {
            "country": "Australia",
            "channel_handle": "@9newsaus",
            "playlist_id": "PLsBzd5S5fFgGBkq06LOg5cZjN-g2YD7HG",
            # 2026-10-28 부터 제목이 '9News Sydney' → '9 News Sydney'(띄어쓰기)로 바뀜 — 둘 다 허용
            "keyword": "9 News Sydney",
            "title_regex": r"9\s?News Sydney\s*\|",
            "save_fields": "subtitle"
        },
        # (2026-10-27 추가) 사우디 — 알하다스(MBC 그룹, 알아라비야 자매채널) '6시 GMT 뉴스' 풀 방송(52~54분, 매일 ~07:00 UTC).
        # 국영 알에크바리야는 풀 뉴스를 유튜브에 안 올림. 제목 철자가 غرينيتش/غرينتش 로 섞이고 4·8·12시판도
        # 같은 형식이라(퍼지 키워드가 4시판까지 0.93 으로 통과) 정규식으로 '6시'만. 전용 재생목록은 2026-08 이후 갱신 중단
        # → 업로드 재생목록(UU…, 1유닛) 우선, 최신 10개에 없으면 search 폴백(나라별 하루 예산 내).
        {
            "country": "SaudiArabia",
            "channel_handle": "@alhadath",
            "playlist_id": "UUrj5BGAhtWxDfqbza9T9hqA",
            "keyword": "نشرة 6 غرينيتش",
            "title_regex": r"نشرة\s*6\s*غرين",
            "save_fields": "subtitle"
        },
    ]

dmi_KR_STOCK_SYMBOLS = {
         "kospi200": "2001",          # 코스피 200
    }

os_INDEX_SYMBOLS = {
    "nasdaq100": "NDX",           # 나스닥 100
    "ShanghaiA": "CH#SHA",           # 상해 A
    "hangseng": "HK#HS",            # 항셍
    "nikkei225": "JP#NI225",          # 닛케이 225
    "BSE30":"IN#BOMBAY", # 인도 센섹스30
    "eurostoxx50": "SX5E",    # 유로스톡스 50
    "dax": "GR#DAX",               # 독일 DAX 30
}

os_treasury_SYMBOLS = {
    "us-t10" : "Y0202",
    "jp-t10" : "Y0207",
    "kr-t3" : "Y0101"
}
"""
dmc_KR_STOCK_SYMBOLS = {
        "삼성전자": "005930",
        "한화오션": "042660",
        "카카오": "035720",
    }

os_US_STOCK_SYMBOLS = {
    "tesla": "TSLA",  # 테슬라
}
"""

yf_CURRENCY_SYMBOLS = {
    "dxy": "DX-Y.NYB",     # 한국 원
}

os_CURRENCY_SYMBOLS = {
    "usd_krw": "FX@KRW",     # 한국 원
    "usd_jpy": "FX@JPY",     # 일본 엔
    "usd_cny": "FX@CNY",     # 중국 위안
    "usd_hkd": "FX@HKD",     # 중국 위안
    "usd_eur": "FX@EUR",     # 유로 유로->달러임
    "usd_gbp": "FX@GBP",     # 영국 파운드 파운드->달러
    "usd_cad": "FX@CAD",     # 캐나다 달러
    "usd_sek": "FX@SEK",     # 스웨덴 크로나
    "usd_chf": "FX@CHF",     # 스위스 프랑
    "usd_inr": "FX@IDR",     # 인도 루피
    "usd_thb": "FX@THB",     # 태국 바트
    "usd_vnd": "FX@VND",     # 베트남 동
    "usd_sgd": "FX@SGD",     # 싱가포르 달러
}

os_COMMODITY_SYMBOLS = {
    "gold": "NYGOLD",                # 금C
    "crude_oil": "WTIF",           # WTI 원유C
    "corn": "CHICORN",  # CBOT 옥수수C
    "coffee": "COFFE",  # NYBOT 커피 C
}

ALL_SYMBOLS = {
#    "domestic": {"kr_stock":dmc_KR_STOCK_SYMBOLS,},
    "overseas": {
        "index" : os_INDEX_SYMBOLS,
        "commodity" : os_COMMODITY_SYMBOLS,
        "currency" : os_CURRENCY_SYMBOLS,
#        "us_stock" : os_US_STOCK_SYMBOLS,
        "treasury" : os_treasury_SYMBOLS
    },
    "dmr": {"index":dmi_KR_STOCK_SYMBOLS,
    }
}

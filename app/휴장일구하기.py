import requests
import datetime
import os
from pathlib import Path
from dotenv import load_dotenv
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)
CALENDARIFIC_API_KEY = os.getenv("CALENDARIFIC_API_KEY")

COUNTRIES = {
    "KR": "한국",
    "CN": "중국",
    "JP": "일본",
    "US": "미국",
    "HK": "홍콩",
    "GB": "영국",
    "DE": "독일",
    "AU": "호주",      # 2026-10-27 뉴스 국가 추가와 함께
    "SA": "사우디",
}


def get_market_holidays():
    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=13)
    holiday_result = {}

    for code, name in COUNTRIES.items():
        try:
            url = "https://calendarific.com/api/v2/holidays"
            params = {
                "api_key": CALENDARIFIC_API_KEY,
                "country": code,
                "year": today.year,
                "type": "national"
            }

            response = requests.get(url, params=params)

            if response.status_code != 200:
                raise Exception(f"API 호출 실패: {response.status_code}")

            data = response.json()
            holidays = data['response']['holidays']

            upcoming = []
            for h in holidays:
                try:
                    h_date = datetime.datetime.fromisoformat(h['date']['iso']).date()
                except ValueError:
                    h_date = datetime.date.fromisoformat(h['date']['iso'][:10])

                if today <= h_date <= end_date:
                    upcoming.append({
                        "date": str(h_date),
                        "name": h['name'],
                        "description": h['description']
                    })

            holiday_result[code] = upcoming

        except Exception as e:
            holiday_result[code] = {"error": str(e)}

    return holiday_result
if __name__ == "__main__":
    from pprint import pprint

    data = get_market_holidays()
    pprint(data)
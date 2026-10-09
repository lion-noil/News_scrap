# indexnow.py — hyeongeonnoil.com 페이지 변경을 검색엔진(Bing/Yandex 등 IndexNow 연합 + 네이버)에 즉시 알림 (2026-10-09)
#
# 왜: 브리핑(/briefing/<day>, /en/briefing/<day>)·아카이브(/archive/<day>)는 하루 한 번 새로 생기는데
#     사이트맵 크롤 주기를 기다리면 하루~며칠 늦게 색인된다. IndexNow 는 URL 목록 POST 한 번으로 즉시 통보.
# 키 파일: https://hyeongeonnoil.com/<KEY>.txt 가 사이트(hyeongeonnoil 레포 public/)에 있어야 검증이 통과한다.
# 실패는 로그만 — 호출 측(브리핑·persist) 흐름을 절대 막지 않는다.
#
# 수동: python indexnow.py /briefing/2026-10-08 /en/briefing/2026-10-08
import os
import sys
import logging

import requests

log = logging.getLogger(__name__)

HOST = "hyeongeonnoil.com"
SITE = f"https://{HOST}"
KEY = "7c1e9b2d4f6a4e0b9c3d5a8e2f1b6d4c"
KEY_LOCATION = f"{SITE}/{KEY}.txt"
ENDPOINTS = [
    "https://api.indexnow.org/indexnow",         # Bing·Yandex·Seznam·Naver 연합(공유)
    "https://searchadvisor.naver.com/indexnow",  # 네이버 직접
]
TIMEOUT_SEC = 8
MAX_URLS = 10000  # IndexNow 1회 상한


def _normalize(urls) -> list[str]:
    """'/path' 또는 전체 URL → 'https://hyeongeonnoil.com/path'. 순서 유지·중복 제거·다른 호스트 제외."""
    out, seen = [], set()
    for u in urls or []:
        if not u or not isinstance(u, str):
            continue
        u = u.strip()
        if u.startswith("/"):
            u = SITE + u
        elif not u.startswith("http"):
            u = f"{SITE}/{u}"
        parts = u.split("/")
        if len(parts) < 3 or parts[2].lower() != HOST:
            log.warning("⚠️ IndexNow: 다른 호스트 URL 제외 %s", u)
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_URLS]


def ping(urls: list[str]) -> dict:
    """URL 목록을 두 엔드포인트에 POST. 반환: {endpoint: status_code|None}. 예외는 모두 삼키고 로그만 남긴다.

    IndexNow 응답: 200 OK · 202 Accepted(키 검증 대기) · 400 잘못된 형식 · 403 키 불일치 · 422 URL/호스트 불일치 · 429 과다.
    INDEXNOW_DISABLED=1 이면 아무것도 보내지 않는다(로컬 테스트용)."""
    results: dict = {}
    try:
        url_list = _normalize(urls)
        if not url_list:
            return results
        if (os.getenv("INDEXNOW_DISABLED") or "").strip() in ("1", "true", "yes"):
            log.info("ℹ️ IndexNow 비활성(INDEXNOW_DISABLED) — %d개 URL 전송 생략", len(url_list))
            return results
        body = {"host": HOST, "key": KEY, "keyLocation": KEY_LOCATION, "urlList": url_list}
        for ep in ENDPOINTS:
            try:
                r = requests.post(ep, json=body, headers={"Content-Type": "application/json; charset=utf-8"},
                                  timeout=TIMEOUT_SEC)
                results[ep] = r.status_code
                if r.status_code in (200, 202):
                    log.info("🔔 IndexNow %s ← %d개 URL (%d)", ep, len(url_list), r.status_code)
                else:
                    log.warning("⚠️ IndexNow %s 응답 %d: %s", ep, r.status_code, (r.text or "")[:200])
            except Exception as e:
                results[ep] = None
                log.warning("⚠️ IndexNow %s 전송 실패: %s", ep, e)
    except Exception as e:  # 절대 호출 측을 깨지 않는다
        log.warning("⚠️ IndexNow 핑 중 예외: %s", e)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:] or ["/"]
    print(ping(args))

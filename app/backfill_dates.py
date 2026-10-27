# backfill_dates.py
"""
결손일 뉴스 재수집 백필.
각국 채널에서 '지정한 날짜(publishedAt KST 기준)'의 뉴스 영상을 찾아 요약해
Supabase daily_collections / youtube_transcripts 에 채운다.

- 기존 헬퍼 재사용: get_channel_id / get_video_details / summarize_content / render_summary_text
- 자막: yt_dlp+faster_whisper 있으면 Whisper, 없으면 영상 description 으로 폴백
- YouTube API 절약: 채널당 후보를 1회 수집(playlistItems 우선, 1유닛) 후 날짜별로 매칭

실행 (News_scrap/app 에서):
  ../.venv/Scripts/python backfill_dates.py --dry-run 2026-07-16 2026-07-17 2026-07-18 2026-07-19
  ../.venv/Scripts/python backfill_dates.py           2026-07-16 2026-07-17 2026-07-18 2026-07-19
옵션:
  --dry-run     탐색만(요약/저장 안 함)
  --no-whisper  자막 스킵, description 만으로 요약
  --overwrite   해당 day 행이 이미 있어도 덮어씀
  --countries   쉼표 구분 국가만 처리 (예: Australia,SaudiArabia)                       (2026-10-27)
  --merge       day 행이 있으면 지정 국가만 기존 youtube_data 에 병합(다른 나라 보존).
                Redis 스냅샷 news:daily_saved_data:YYYYMMDD 가 남아 있으면 거기도 병합(재persist 대비).
                이미 있는 나라는 --overwrite 일 때만 교체.
  --search      재생목록 후보와 별개로 search.list(100유닛)도 함께 조회 — 업로드가 많은 채널의
                며칠 전 영상은 재생목록 최신 N개에 없기 때문(알하다스 등)
신규 국가 첫날 백필 예:
  python backfill_dates.py 2026-10-26 --countries Australia,SaudiArabia --merge --search
"""
import os
import re
import sys
import json
import argparse
from difflib import SequenceMatcher
from datetime import datetime, timezone

import requests
import isodate
from pytz import timezone as _tz

# 기존 헬퍼 재사용 (모듈 import 시 .env 로드됨)
from URL과요약문만들기 import (
    get_channel_id,
    summarize_content,
    render_summary_text,
)
from persist import get_supabase

SEOUL = _tz("Asia/Seoul")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DUR_MIN, DUR_MAX = 300, 7200  # 5분~2시간 (기존 로직과 동일)


def kst_date_of(pub_utc: str):
    return (
        datetime.strptime(pub_utc, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .astimezone(SEOUL)
        .date()
    )


def _title_matches(title: str, keyword: str) -> float:
    t, k = title.lower(), keyword.lower().strip()
    if not k:
        return 1.0
    if k in t:
        return 1.0
    best = 0.0
    for i in range(max(1, len(t) - len(k) + 1)):
        best = max(best, SequenceMatcher(None, k, t[i:i + len(k)]).ratio())
    return best


def collect_candidate_ids(channel, max_results=25, force_search=False):
    """채널 후보 video_id 목록 (playlistItems 우선, 없거나 force_search 면 search 도)."""
    channel_id = get_channel_id(channel["channel_handle"])
    if not channel_id:
        print(f"  ❌ {channel['country']}: 채널ID 못 찾음")
        return []
    playlist_ids = channel["playlist_id"]
    playlist_ids = playlist_ids if isinstance(playlist_ids, list) else [playlist_ids]

    ids = []
    for pid in playlist_ids:
        if not pid:
            continue
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet", "maxResults": max_results, "playlistId": pid, "key": YOUTUBE_API_KEY},
            timeout=15,
        )
        for it in r.json().get("items", []):
            vid = it.get("snippet", {}).get("resourceId", {}).get("videoId")
            if vid and vid not in ids:
                ids.append(vid)

    # playlist 비어있거나 결과 없으면 search 폴백 (search.list = 100유닛). force_search 면 무조건 합친다.
    if not ids or force_search:
        keywords = channel["keyword"] if isinstance(channel["keyword"], list) else [channel["keyword"]]
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "maxResults": max_results, "channelId": channel_id,
                    "q": keywords[0], "type": "video", "order": "date", "key": YOUTUBE_API_KEY},
            timeout=15,
        )
        for it in r.json().get("items", []):
            vid = it.get("id", {}).get("videoId")
            if vid and vid not in ids:
                ids.append(vid)
    return ids


def fetch_details(video_ids):
    """videos.list 배치(최대 50개, 1유닛)로 snippet+contentDetails 조회."""
    out = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet,contentDetails", "id": ",".join(chunk), "key": YOUTUBE_API_KEY},
            timeout=15,
        )
        for it in r.json().get("items", []):
            out[it["id"]] = it
    return out


def build_channel_index(channel, max_results=25, force_search=False):
    """채널의 후보 영상들을 [{video_id,title,publishedAt,description,duration}] 로."""
    ids = collect_candidate_ids(channel, max_results, force_search=force_search)
    details = fetch_details(ids)
    rows = []
    for vid, v in details.items():
        try:
            dur = isodate.parse_duration(v["contentDetails"]["duration"]).total_seconds()
        except Exception:
            dur = 0
        rows.append({
            "video_id": vid,
            "title": v["snippet"]["title"],
            "publishedAt": v["snippet"]["publishedAt"],
            "description": v["snippet"].get("description", ""),
            "duration": dur,
        })
    return rows


def pick_for_date(rows, keyword_list, target_date, title_regex=None):
    """target_date(KST)에 맞는 후보 중 키워드/길이 조건 최적 1개. title_regex(채널 설정)가 있으면 그것도 통과해야 함."""
    kws = keyword_list if isinstance(keyword_list, list) else [keyword_list]
    cands = []
    for row in rows:
        if kst_date_of(row["publishedAt"]) != target_date:
            continue
        if not (DUR_MIN <= row["duration"] <= DUR_MAX):
            continue
        if title_regex and not re.search(title_regex, row["title"]):
            continue
        score = max(_title_matches(row["title"], kw) for kw in kws)
        cands.append((score, row["duration"], row))
    if not cands:
        return None
    # 키워드 점수 우선, 동점이면 긴 영상(풀에피소드)
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[0][2]


def get_transcript_safe(video_id, use_whisper):
    if not use_whisper:
        return None
    try:
        from URL과요약문만들기 import get_transcript_text
        return get_transcript_text(video_id)
    except Exception as e:
        print(f"    ⚠️ Whisper 불가({e}) → description 사용")
        return None


def _merge_redis_snapshot(day_str, new_data):
    """news:daily_saved_data:YYYYMMDD(Upstash, TTL 10일) 가 남아 있으면 새 나라를 병합 — 이후 재persist 때 사라지지 않게.
    new_data 값은 summary_content(자막) 포함 원형(storage.save_daily_data 와 같은 모양). TTL 은 유지."""
    try:
        from redis_client import redis_client
        key = f"news:daily_saved_data:{day_str.replace('-', '')}"
        raw = redis_client.get(key)
        if not raw:
            print(f"  ℹ️ Redis 스냅샷 {key} 없음(만료) → Supabase 만 반영")
            return
        snap = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        snap.setdefault("youtube_data", {}).update(new_data)
        ttl = redis_client.ttl(key)
        payload = json.dumps(snap, ensure_ascii=False)
        if ttl and ttl > 0:
            redis_client.set(key, payload, ex=int(ttl))
        else:
            redis_client.set(key, payload)
        print(f"  ✅ Redis 스냅샷 병합 {key} (+{', '.join(new_data)}; 총 {len(snap['youtube_data'])}개국, TTL {ttl}s)")
    except Exception as e:
        print(f"  ⚠️ Redis 스냅샷 병합 실패(무시, Supabase 는 반영됨): {e}")


def process_date(target_date, channel_indexes, channels, dry_run, use_whisper, overwrite, merge=False):
    day_str = target_date.strftime("%Y-%m-%d")
    supabase = None if dry_run else get_supabase()

    existing_yd = None   # merge 모드에서 기존 행의 youtube_data
    if not dry_run:
        exist = supabase.table("daily_collections").select("day,raw_json").eq("day", day_str).execute()
        if exist.data:
            if merge:
                existing_yd = ((exist.data[0].get("raw_json") or {}).get("youtube_data") or {})
                print(f"\n[{day_str}] 기존 행 병합 모드 — 이미 있는 나라: {sorted(existing_yd)}")
            elif not overwrite:
                print(f"\n[{day_str}] 이미 존재 → 스킵 (--overwrite 로 덮기, --merge 로 나라 추가)")
                return

    print(f"\n===== [{day_str}] =====")
    youtube_data = {}
    raw_for_snapshot = {}
    transcript_rows = []

    for ch in channels:
        country = ch["country"]
        if existing_yd is not None and country in existing_yd and not overwrite:
            print(f"  {country:11s}: 이미 있음 → 유지 (--overwrite 로 교체)")
            continue
        vid = pick_for_date(channel_indexes[country], ch["keyword"], target_date, ch.get("title_regex"))
        if not vid:
            print(f"  {country:9s}: ✗ 해당 날짜 영상 없음")
            continue
        print(f"  {country:9s}: ✓ {vid['title'][:46]}  ({vid['publishedAt']}, {int(vid['duration'])}s)")

        if dry_run:
            youtube_data[country] = {"url": f"https://www.youtube.com/watch?v={vid['video_id']}",
                                     "title": vid["title"], "publishedAt": vid["publishedAt"]}
            continue

        content = get_transcript_safe(vid["video_id"], use_whisper) or vid["description"]
        if not content or not content.strip():
            print(f"    ⚠️ 요약할 텍스트 없음, 스킵")
            continue
        items = summarize_content(content)
        if not items:
            print(f"    ⚠️ 요약 실패, 스킵")
            continue

        processed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        youtube_data[country] = {
            "url": f"https://www.youtube.com/watch?v={vid['video_id']}",
            "title": vid["title"],
            "publishedAt": vid["publishedAt"],
            "summary_target": ch.get("save_fields"),
            "summary_items": items,
            "summary_result": render_summary_text(items),
            "processed_time": processed,
        }
        raw_for_snapshot[country] = {**youtube_data[country], "summary_content": content}
        transcript_rows.append({"day": day_str, "country": country, "summary_content": content})
        print(f"    ✅ 요약 완료 ({len(items)}건)")

    if dry_run:
        print(f"[{day_str}] DRY-RUN: {len(youtube_data)}개국 발견")
        return
    if not youtube_data:
        print(f"[{day_str}] 저장할 데이터 없음")
        return

    if transcript_rows:
        supabase.table("youtube_transcripts").upsert(transcript_rows, on_conflict="day,country").execute()

    if existing_yd is not None:
        # 병합: 쓰기 직전에 다시 읽어 그 사이 다른 쓰기(persist 등)를 덮지 않게 한다.
        cur = supabase.table("daily_collections").select("raw_json").eq("day", day_str).execute()
        raw_json = (cur.data[0].get("raw_json") if cur.data else None) or {}
        merged = {**(raw_json.get("youtube_data") or {}), **youtube_data}
        raw_json = {**raw_json, "youtube_data": merged}
        supabase.table("daily_collections").upsert({
            "day": day_str, "raw_json": raw_json,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="day").execute()
        print(f"[{day_str}] ✅ Supabase 병합 완료 (+{', '.join(youtube_data)} → 총 {len(merged)}개국, transcripts {len(transcript_rows)})")
        _merge_redis_snapshot(day_str, raw_for_snapshot)
        return

    supabase.table("daily_collections").upsert({
        "day": day_str,
        "raw_json": {"youtube_data": youtube_data},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="day").execute()
    print(f"[{day_str}] ✅ Supabase 저장 완료 ({len(youtube_data)}개국, transcripts {len(transcript_rows)})")


def main():
    from test_config import channels

    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+", help="YYYY-MM-DD ...")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--max-results", type=int, default=25)
    ap.add_argument("--countries", default="", help="쉼표 구분 국가만 (예: Australia,SaudiArabia)")
    ap.add_argument("--merge", action="store_true", help="기존 day 행에 지정 국가만 병합")
    ap.add_argument("--search", action="store_true", help="재생목록과 별개로 search.list 도 조회")
    args = ap.parse_args()
    if args.countries:
        want = {c.strip() for c in args.countries.split(",") if c.strip()}
        unknown = want - {c["country"] for c in channels}
        if unknown:
            sys.exit(f"알 수 없는 국가: {sorted(unknown)}")
        channels = [c for c in channels if c["country"] in want]

    dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in args.dates]

    # whisper 가용성 자동 감지
    use_whisper = not args.no_whisper
    if use_whisper:
        try:
            import yt_dlp  # noqa
            import faster_whisper  # noqa
        except Exception:
            print("ℹ️ yt_dlp/faster_whisper 없음 → Whisper 스킵, description 으로 요약합니다.\n")
            use_whisper = False

    print(f"채널 후보 수집 중... (채널 {len(channels)}개)")
    channel_indexes = {}
    for ch in channels:
        rows = build_channel_index(ch, args.max_results, force_search=args.search)
        channel_indexes[ch["country"]] = rows
        print(f"  {ch['country']:9s}: 후보 {len(rows)}개")

    for d in dates:
        process_date(d, channel_indexes, channels, args.dry_run, use_whisper, args.overwrite, merge=args.merge)

    print("\n완료.")


if __name__ == "__main__":
    main()

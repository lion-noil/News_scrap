from difflib import SequenceMatcher
from pathlib import Path
from dotenv import load_dotenv
import requests
import os
import json
from playwright.sync_api import TimeoutError as PWTimeout
import sys
import asyncio
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import isodate
import re
import llm  # 구독 Claude(claude -p) → 실패 시 OpenAI 폴백 (2026-09-12)
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")  # .env에서 불러오기

from datetime import datetime

def get_channel_id(channel_handle):
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "id",
        "forHandle": channel_handle,
        "key": YOUTUBE_API_KEY
    }
    response = requests.get(url, params=params)
    return response.json().get("items", [{}])[0].get("id")

def get_video_details(video_id):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }
    response = requests.get(url, params=params)
    items = response.json().get("items")
    return items[0] if items else None

_whisper_model = None

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        import ctranslate2
        from faster_whisper import WhisperModel
        if ctranslate2.get_cuda_device_count() > 0:
            device, compute_type = "cuda", "float16"
        else:
            device, compute_type = "cpu", "int8"
        print(f"Whisper 디바이스: {device} ({compute_type})")
        _whisper_model = WhisperModel("small", device=device, compute_type=compute_type)
    return _whisper_model

def get_transcript_text(video_id, headless=True):
    import tempfile
    import glob as _glob
    import yt_dlp

    print(f"[{video_id}] 음성 다운로드 중...")
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, f"{video_id}.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "32",
            }],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as e:
            err_str = str(e)
            if "Private video" in err_str or "Video unavailable" in err_str or "This video is not available" in err_str:
                print(f"[{video_id}] 비공개/삭제된 영상, 스킵")
            else:
                print(f"[{video_id}] 다운로드 실패: {e}")
            return None

        audio_files = _glob.glob(os.path.join(tmpdir, "*.mp3"))
        if not audio_files:
            print(f"[{video_id}] 오디오 파일 없음")
            return None

        print(f"[{video_id}] 다운로드 완료, Whisper 변환 시작...")
        try:
            result = _transcribe_chunked(audio_files[0], video_id)
            if result:
                print(f"[{video_id}] Whisper 변환 완료 ({len(result)}자)")
            return result
        except Exception as e:
            print(f"Whisper 변환 실패 ({video_id}): {e}")
            return None


# 긴 방송(1~2시간)을 한 번에 transcribe 하면 faster-whisper 가 오디오 전체의 STFT 를
# complex128 로 한꺼번에 계산해 RSS 가 3GB 를 넘고 컨테이너가 OOM-kill(137) 된다.
# (2026-09-13~17: KBS 1h47m·56m 영상에서 하루 수백 회 재시작 루프 → 타국 수집 전부 차단,
#  전일 브리핑이 홍콩 1개국 입력으로 생성되는 사고)
# → 오디오를 메모리에 한 번만 디코드한 뒤 WHISPER_CHUNK_SEC 단위로 잘라 순차 변환한다.
#   20분 조각의 특징추출 피크는 1GB 미만. 언어는 첫 조각에서 판정해 나머지 조각에 고정
#   (조각마다 재판정하면 중간 외국어 인용 구간에서 언어가 튄다).
WHISPER_CHUNK_SEC = 20 * 60
WHISPER_SR = 16000


def _transcribe_chunked(audio_path, video_id):
    from faster_whisper.audio import decode_audio

    model = _get_whisper_model()
    audio = decode_audio(audio_path, sampling_rate=WHISPER_SR)
    total = len(audio)
    step = WHISPER_CHUNK_SEC * WHISPER_SR
    n_chunks = max(1, -(-total // step))
    print(f"[{video_id}] 오디오 {total / WHISPER_SR / 60:.1f}분 → {n_chunks}조각({WHISPER_CHUNK_SEC // 60}분) 순차 변환")

    language = None
    texts = []
    for i in range(n_chunks):
        chunk = audio[i * step:(i + 1) * step]
        if len(chunk) < WHISPER_SR:  # 1초 미만 꼬리는 무시
            continue
        segments, info = model.transcribe(chunk, beam_size=5, language=language)
        if language is None:
            language = info.language
        piece = " ".join(seg.text.strip() for seg in segments)
        texts.append(piece)
        print(f"[{video_id}] 조각 {i + 1}/{n_chunks} 완료 ({len(piece)}자, lang={language})")
    del audio
    transcript = " ".join(t for t in texts if t).strip()
    return transcript or None

def open_transcript_ui(page):
    try:
        page.click("tp-yt-paper-button#expand", timeout=3000)
    except:
        pass
    try:
        page.locator("button[aria-label='스크립트 표시']").first.click(timeout=5000)
    except:
        pass

def capture_get_transcript_request(page, trigger_fn, timeout_ms=8000):
    captured = {}

    def handler(route, request):
        if "/youtubei/v1/get_transcript" in request.url:
            captured["url"] = request.url
            captured["body"] = request.post_data
            captured["headers"] = request.headers
            route.continue_()
        else:
            route.continue_()

    page.route("**/*", handler)

    trigger_fn()  # 스크립트 표시 클릭

    page.wait_for_timeout(timeout_ms)
    page.unroute("**/*", handler)

    return captured if "url" in captured else None

def replay_get_transcript(page, captured):
    headers = {
        "content-type": "application/json",
    }
    resp = page.request.post(
        captured["url"],
        data=captured["body"],
        headers=headers,
    )
    return resp

def extract_text(j):
    out = []
    def walk(x):
        if isinstance(x, dict):
            if "simpleText" in x:
                out.append(x["simpleText"])
            if "runs" in x:
                for r in x["runs"]:
                    if "text" in r:
                        out.append(r["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(j)
    return "\n".join(t.strip() for t in out if t.strip())



def title_ok(title, title_regex=None):
    """채널별 제목 정규식(선택) — 퍼지 키워드만으론 못 거르는 같은 형식의 다른 편을 배제.
    예) 알하다스 'نشرة 6 غرينيتش' 퍼지 매칭이 'نشرة 4 غرينيتش'(4시 GMT, 25분)까지 0.93 으로 통과(2026-10-27 실측)."""
    if not title_regex:
        return True
    return re.search(title_regex, title or "") is not None


def find_best_video(data, keyword, from_playlist=False, title_regex=None):
    """키워드와 제목이 맞고 길이 5분~2시간인 첫 영상 → (video_id, publishedAt UTC 문자열). 없으면 (None, None).
    title_regex 가 있으면 원 제목이 그 정규식에도 맞아야 한다."""
    for item in data.get("items", []):

        vid_id = None
        if isinstance(item.get("id"), dict):
            vid_id = item["id"].get("videoId")
        elif isinstance(item.get("snippet", {}).get("resourceId"), dict):
            vid_id = item["snippet"]["resourceId"].get("videoId")
        if not title_ok(item["snippet"]["title"], title_regex):
            continue
        title = item["snippet"]["title"].lower()
        if any(SequenceMatcher(None, keyword.lower(), title[i:i+len(keyword)]).ratio() > 0.9
               for i in range(len(title) - len(keyword) + 1)):
            video = get_video_details(vid_id)
            if not video:
                continue
            try:
                duration = isodate.parse_duration(video["contentDetails"]["duration"])
                if 300 <= duration.total_seconds() <= 7200:
                    return vid_id, video["snippet"].get("publishedAt")
            except Exception as e:
                print(f"⏱ duration 파싱 실패: {e}")
    return None, None


# 재생목록 후보가 이 시간보다 오래됐으면 search.list 로 보강한다.
# (BBC Newscast 재생목록은 오래된 순이라 앞 10개가 2025년 영상, PBS 도 최신 편이 앞쪽에 없음 — 2026-09-19 실측)
PLAYLIST_FRESH_HOURS = 24


def _age_hours(published_utc_str):
    try:
        dt = datetime.strptime(published_utc_str, "%Y-%m-%dT%H:%M:%SZ")
        return (datetime.utcnow() - dt).total_seconds() / 3600
    except Exception:
        return float("inf")
def search_video_ids(channel_id, playlist_id, keyword, allow_search=True, stats=None, title_regex=None):
    """재생목록(1유닛) 먼저. 후보가 없거나 PLAYLIST_FRESH_HOURS 보다 오래됐을 때만 search.list(100유닛) 보강.
    2026-09-19 수집 창 24시간화에 맞춘 쿼터 절감 — 종전엔 매 시도마다 search+playlist 둘 다(≈101유닛)라
    11시간 창에서도 하루 쿼터(10,000) 근처였다. allow_search=False 면 재생목록만(호출측이 시간대·예산으로 제어).
    stats(dict)가 오면 stats['search_calls'] 에 search 호출 수를 누적. 반환 후보 중 최신은 호출측이 고른다."""
    results = []
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        params={"part": "snippet", "maxResults": 10, "key": YOUTUBE_API_KEY, "playlistId": playlist_id},
    )
    vid_id, pub = find_best_video(resp.json(), keyword, from_playlist=True, title_regex=title_regex)
    if vid_id:
        results.append(vid_id)
        if _age_hours(pub) <= PLAYLIST_FRESH_HOURS:
            return results
    if not allow_search:
        return results
    if stats is not None:
        stats["search_calls"] = stats.get("search_calls", 0) + 1
    # order=date: 기본(relevance)은 며칠 지난 편을 앞에 두어 최신 편을 놓친다(PBS 9/17편 있는데 9/16편 반환 — 2026-09-19 실측)
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={"part": "snippet", "maxResults": 25, "order": "date", "type": "video",
                "key": YOUTUBE_API_KEY, "channelId": channel_id, "q": keyword},
    )
    vid_id, _ = find_best_video(resp.json(), keyword, from_playlist=False, title_regex=title_regex)
    if vid_id and vid_id not in results:
        results.append(vid_id)
    return results

# 한 영상에서 뽑을 최대 뉴스 개수
MAX_NEWS_ITEMS = 5

SUMMARY_SYSTEM = "너는 뉴스 방송 자막을 읽고 핵심 뉴스를 선별·요약하는 편집자다. 요청된 JSON 객체만 출력한다."


def _summary_schema():
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rank": {"type": "integer"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "points": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["rank", "title", "summary", "points"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def summarize_content(content):
    """
    뉴스 전체 텍스트 -> 중요도 순위별 구조화 요약(list[dict]).

    반환:
      [{"rank": 1, "title": ..., "summary": ..., "points": [...]}, ...]
      또는 실패 시 None

    NOTE: 반환 타입이 list(구조화)로 바뀌었음.
          기존 텍스트 포맷(summary_result)이 필요하면 render_summary_text(items) 사용.
    """
    if content is None:
        print("contents 없음")
        return None
    if len(content) > 300000:
        print("300,000 이상 길이 요약내용")
        return None
    if not content.strip():  # 공백만 있는 경우
        print("contents 전체 공백")
        return None
    try:
        prompt = (
                content.strip()
                + "\n\n---\n\n"
                + "위 뉴스 전체 내용을 기반으로 사회적 파급력, 정치·경제적 영향, 국제적 관심도를 기준으로 "
                + f"가장 중요한 뉴스를 최대 {MAX_NEWS_ITEMS}개까지 중요도 순으로 선별해 JSON으로만 출력해줘.\n"
                + "형식(JSON):\n"
                + '{ "items": [ { "rank": 1, '
                + '"title": "뉴스 제목 혹은 주제 요약", '
                + '"summary": "핵심 사건을 한 문장으로", '
                + '"points": ["주요 쟁점1", "주요 쟁점2", "주요 쟁점3"] } ] }\n'
                + "- rank는 1부터 시작하는 중요도 순위(중복 없이, 1이 가장 중요).\n"
                + f"- items는 최대 {MAX_NEWS_ITEMS}개. 뉴스가 적으면 더 적어도 됨.\n"
                + "- points는 항목당 2~4개.\n"
                + "- 모든 문자열은 반드시 한글, 한국어로만 작성해.\n"
                + "- JSON 외의 다른 텍스트는 절대 출력하지 마."
        )

        # Claude(구독) 우선, 실패 시 OpenAI(json_object, 종전 방식) 폴백 — llm.structured 내부에서 처리
        data = llm.structured(
            "summary", prompt, _summary_schema(), system=SUMMARY_SYSTEM,
            openai_response_format={"type": "json_object"},
        )
        items = _coerce_summary_items(data)
        return items or None

    except Exception as e:
        print(f"오류 발생: {e}")
        return None


def _coerce_summary_items(raw):
    """LLM이 돌려준 JSON(문자열 또는 이미 파싱된 dict/list) -> 정규화된 items 리스트."""
    if not raw:
        return []
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except Exception as e:
            print(f"요약 JSON 파싱 실패: {e}")
            return []

    if isinstance(data, list):
        arr = data
    elif isinstance(data, dict):
        arr = data.get("items") or data.get("news") or data.get("results") or []
    else:
        arr = []

    out = []
    for i, it in enumerate(arr):
        if not isinstance(it, dict):
            continue

        title = str(it.get("title") or it.get("headline") or "").strip()
        summary = str(
            it.get("summary") or it.get("one_line") or it.get("한줄요약") or ""
        ).strip()

        points_raw = it.get("points") or it.get("쟁점") or it.get("key_points") or []
        if isinstance(points_raw, str):
            points = [points_raw.strip()] if points_raw.strip() else []
        elif isinstance(points_raw, list):
            points = [str(p).strip() for p in points_raw if str(p).strip()]
        else:
            points = []

        if not (title or summary or points):
            continue

        try:
            rank = int(it.get("rank"))
        except (TypeError, ValueError):
            rank = i + 1

        out.append({"rank": rank, "title": title, "summary": summary, "points": points})

    # 중요도(rank) 정렬 후 1..N 재부여, 최대 MAX_NEWS_ITEMS개로 제한
    out.sort(key=lambda x: x.get("rank", 999))
    out = out[:MAX_NEWS_ITEMS]
    for idx, it in enumerate(out):
        it["rank"] = idx + 1
    return out


def render_summary_text(items):
    """
    구조화 items -> 기존 summary_result 텍스트 포맷으로 렌더.
    세계정세분석.py / 아카이브 <pre> / 복사 기능 호환을 위해 유지.
    """
    if not items:
        return None
    blocks = []
    for it in items:
        rank = it.get("rank")
        title = (it.get("title") or "").strip()
        summary = (it.get("summary") or "").strip()
        points = it.get("points") or []

        lines = [f"{rank}. 🗞️ {title}"]
        if summary:
            lines.append(f"✅ 한줄 요약: {summary}")
        if points:
            lines.append("🔥 주요 쟁점:")
            for j, p in enumerate(points, start=1):
                lines.append(f"    {j}) {p}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

def find_similar_video_title_id(data, keyword, similarity_threshold=0.9,from_playlist=False):
    try:
        items = data["items"]
    except KeyError as e:
        print(f"❌ 'items' 필드가 없습니다: {str(e)}")
        return None
    keyword = keyword.lower()
    k_len = len(keyword)

    for item in data["items"]:
        if isinstance(item["id"], dict):
            video_id = item["id"].get("videoId")
        else:
            video_id = item["snippet"]["resourceId"]["videoId"]
        title = item["snippet"]["title"]
        text = title.lower()
        max_sim = 0.0

        # 슬라이딩 윈도우로 유사도 측정
        for i in range(len(text) - k_len + 1):
            window = text[i:i + k_len]
            sim = SequenceMatcher(None, keyword, window).ratio()
            if sim > max_sim:
                max_sim = sim
                # 유사도가 threshold를 넘으면 그때 duration 확인
                if max_sim > similarity_threshold:
                    videos_check_url = "https://www.googleapis.com/youtube/v3/videos"
                    params = {
                        "part": "contentDetails",
                        "id": video_id,
                        "key": YOUTUBE_API_KEY
                    }
                    response = requests.get(videos_check_url, params=params)
                    video_data = response.json()

                    try:
                        duration = isodate.parse_duration(video_data["items"][0]["contentDetails"]['duration'])
                        # 10분 ~ 2시간 사이만 허용
                        if 300 <= duration.total_seconds() <= 7200:
                            return video_id
                    except (KeyError, IndexError, ValueError) as e:
                        print(f"duration 정보 없는 id {video_id}: {e}")
                        continue
    return None  # 찾는 영상이 없을 경우

def get_latest_video_data(channel, headless=True, allow_search=True, stats=None):
    channel_id = get_channel_id(channel["channel_handle"])
    if not channel_id:
        print("❌ 채널 ID를 찾을 수 없습니다.")
        return None

    keywords = channel["keyword"] if isinstance(channel["keyword"], list) else [channel["keyword"]]
    playlist_ids = channel["playlist_id"] if isinstance(channel["playlist_id"], list) else [channel["playlist_id"]]

    latest = {"time": None, "data": None}

    for i, keyword in enumerate(keywords):
        playlist_id = playlist_ids[i] if i < len(playlist_ids) else playlist_ids[-1]
        for video_id in search_video_ids(channel_id, playlist_id, keyword, allow_search=allow_search, stats=stats,
                                         title_regex=channel.get("title_regex")):
            video = get_video_details(video_id)
            if not video:
                continue
            pb_time = datetime.strptime(video["snippet"]["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")
            if not latest["time"] or pb_time > latest["time"]:
                latest.update({
                    "time": pb_time,
                    "video_id": video_id,
                    "data": {
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": video["snippet"]["title"],
                        "publishedAt": video["snippet"]["publishedAt"],
                        "summary_target": channel["save_fields"],
                        "summary_content": video["snippet"]["description"]
                    }
                })

    pass


    return latest["data"]



if __name__ == "__main__":
    from test_config import channels

    SHOW_CHROME = True

    results = {}
    for channel in channels:
        country = channel["country"]

        video_data = get_latest_video_data(channel, headless=not SHOW_CHROME)

        items = summarize_content(video_data['summary_content'])
        video_data['summary_items'] = items
        video_data['summary_result'] = render_summary_text(items)
        results[country] = video_data

    print(results)

"""국립국어원 공식 Open API 기반 끝말잇기 한방단어 검색기."""
from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request

try:
    from dotenv import load_dotenv
except ImportError:  # requirements 설치 전에도 환경 변수 방식으로 실행 가능
    def load_dotenv() -> bool:
        return False

load_dotenv()

app = Flask(__name__)

PAGE_SIZE = 24
API_PAGE_SIZE = 100
MAX_CANDIDATES = 300
MAX_QUERY_LENGTH = 20
CACHE_TTL = 60 * 30
REQUEST_TIMEOUT = (10, 20)
REQUEST_ATTEMPTS = 2

DUEUM_MAP = {
    "녀": "여", "뇨": "요", "뉴": "유", "니": "이",
    "랴": "야", "려": "여", "례": "예", "료": "요",
    "류": "유", "리": "이", "라": "나", "래": "내",
    "로": "노", "뢰": "뇌", "루": "누", "르": "느",
}

DICTIONARIES = {
    "stdict": {
        "name": "표준국어대사전",
        "endpoint": "https://stdict.korean.go.kr/api/search.do",
        "key_env": "STDICT_API_KEY",
        "detail": "https://stdict.korean.go.kr/search/searchView.do?word_no={target_code}",
    },
    "opendict": {
        "name": "우리말샘",
        "endpoint": "https://opendict.korean.go.kr/api/search",
        "key_env": "OPENDICT_API_KEY",
        "detail": "https://opendict.korean.go.kr/dictionary/view?sense_no={target_code}",
    },
}


class ApiError(RuntimeError):
    pass


class TTLCache:
    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._items: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple):
        with self._lock:
            item = self._items.get(key)
            if not item or time.monotonic() - item[0] >= self.ttl:
                self._items.pop(key, None)
                return None
            return item[1]

    def set(self, key: tuple, value: Any):
        with self._lock:
            self._items[key] = (time.monotonic(), value)


cache = TTLCache()


def get_dueum_variants(syllable: str) -> list[str]:
    """원음과 요청서에 정의된 두음법칙 변환음을 반환한다."""
    return list(dict.fromkeys([syllable, DUEUM_MAP.get(syllable, syllable)]))


def last_hangul_syllable(word: str) -> str:
    matches = re.findall(r"[가-힣]", word or "")
    return matches[-1] if matches else ""


def clean_word(word: str) -> str:
    return re.sub(r"[\-^\s]", "", word or "").strip()


def validate_query(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("검색할 한글 글자나 단어를 입력해 주세요.")
    if len(value) > MAX_QUERY_LENGTH:
        raise ValueError(f"검색어는 {MAX_QUERY_LENGTH}자 이하로 입력해 주세요.")
    if not re.fullmatch(r"[가-힣]+", value):
        raise ValueError("완성된 한글 글자만 입력해 주세요.")
    return value


def as_bool(name: str, default: bool = False) -> bool:
    return request.args.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass
class Filters:
    noun_only: bool = False
    include_proper: bool = False
    include_north: bool = False
    include_dialect: bool = False
    include_old: bool = False
    include_technical: bool = False
    include_single: bool = False

    def key(self) -> tuple:
        return tuple(vars(self).values())


def scalar(value: Any, default: str = "") -> str:
    if isinstance(value, dict):
        value = value.get("#text", value.get("text", default))
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value if value is not None else default).strip()


def parse_json(data: dict, dictionary: str) -> tuple[list[dict], int]:
    channel = data.get("channel", data)
    raw_items = channel.get("item", []) if isinstance(channel, dict) else []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    total = int(scalar(channel.get("total", 0), "0") or 0)
    return [normalize_item(item, dictionary) for item in raw_items], total


def parse_xml(text: str, dictionary: str) -> tuple[list[dict], int]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ApiError("사전에서 올바르지 않은 응답을 받았습니다.") from exc
    error = root.findtext(".//error") or root.findtext(".//message")
    if error and not root.findall(".//item"):
        raise ApiError(error)
    total = int(root.findtext(".//total") or 0)
    items = []
    for node in root.findall(".//item"):
        item = {child.tag: child.text or "" for child in node}
        sense = node.find("sense")
        if sense is not None:
            item["sense"] = {child.tag: child.text or "" for child in sense}
        items.append(normalize_item(item, dictionary))
    return items, total


def normalize_item(item: dict, dictionary: str) -> dict:
    sense = item.get("sense") or {}
    if isinstance(sense, list):
        sense = sense[0] if sense else {}
    if not isinstance(sense, dict):
        sense = {}
    word = clean_word(scalar(item.get("word")))
    code = scalar(item.get("target_code") or sense.get("target_code"))
    detail = scalar(item.get("link"))
    if not detail and code:
        detail = DICTIONARIES[dictionary]["detail"].format(target_code=quote(code))
    return {
        "word": word,
        "part_of_speech": scalar(sense.get("pos") or item.get("pos"), "품사 미상"),
        "definition": scalar(sense.get("definition") or item.get("definition"), "뜻풀이 정보가 없습니다."),
        "category": scalar(sense.get("category") or item.get("category")),
        "type": scalar(sense.get("type") or item.get("type")),
        "dictionary_codes": [dictionary],
        "detail_url": detail,
    }


def allowed(word: dict, filters: Filters) -> bool:
    word_text = word["word"]
    if not word_text or not last_hangul_syllable(word_text):
        return False
    if not filters.include_single and len(re.findall(r"[가-힣]", word_text)) == 1:
        return False
    pos, category, kind = word["part_of_speech"], word["category"], word["type"]
    joined = f"{pos} {category} {kind}"
    if filters.noun_only and "명사" not in pos:
        return False
    exclusions = [
        (filters.include_proper, "고유 명사"), (filters.include_north, "북한어"),
        (filters.include_dialect, "방언"), (filters.include_old, "옛말"),
    ]
    if any(not enabled and marker in joined for enabled, marker in exclusions):
        return False
    if not filters.include_technical and category and category not in {"일반", ""}:
        return False
    return True


def fetch_dictionary(dictionary: str, query: str, start: int, count: int, filters: Filters) -> tuple[list[dict], int]:
    config = DICTIONARIES[dictionary]
    key = os.getenv(config["key_env"], "").strip()
    if not key:
        raise ApiError(f"{config['name']} API 키가 설정되지 않았습니다.")
    cache_key = (dictionary, query, "start", filters.key(), start, count)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {"key": key, "q": query, "req_type": "json", "type_search": "search", "method": "start", "start": start, "num": count, "advanced": "y"}
    last_error: requests.RequestException | None = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = requests.get(config["endpoint"], params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < REQUEST_ATTEMPTS:
                time.sleep(0.35)
    else:
        if isinstance(last_error, requests.Timeout):
            raise ApiError(f"{config['name']} 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.") from last_error
        raise ApiError(f"{config['name']}에 연결할 수 없습니다.") from last_error
    try:
        data = response.json()
        if isinstance(data, dict) and (data.get("error") or data.get("message")) and not data.get("channel"):
            raise ApiError(scalar(data.get("error") or data.get("message")))
        result = parse_json(data, dictionary)
    except ValueError:
        result = parse_xml(response.text, dictionary)
    result = ([word for word in result[0] if allowed(word, filters)], result[1])
    cache.set(cache_key, result)
    return result


def selected_dictionaries(value: str) -> list[str]:
    if value == "both":
        return ["stdict", "opendict"]
    if value not in DICTIONARIES:
        raise ValueError("올바른 사전을 선택해 주세요.")
    return [value]


def merged_search(dictionaries: list[str], query: str, filters: Filters, limit: int = MAX_CANDIDATES) -> tuple[list[dict], int, list[str]]:
    merged: dict[str, dict] = {}
    totals, warnings = 0, []
    for dictionary in dictionaries:
        try:
            words, total = [], 0
            api_start = 1
            # 공식 API의 num 상한에 맞춰 필요한 범위만 페이지 단위로 가져온다.
            while len(words) < limit:
                batch, reported_total = fetch_dictionary(dictionary, query, api_start, min(API_PAGE_SIZE, limit - len(words)), filters)
                total = reported_total
                words.extend(batch)
                if api_start + API_PAGE_SIZE > reported_total or not batch:
                    break
                api_start += API_PAGE_SIZE
            totals += total
            for word in words:
                current = merged.get(word["word"])
                if current:
                    current["dictionary_codes"].extend(x for x in word["dictionary_codes"] if x not in current["dictionary_codes"])
                else:
                    merged[word["word"]] = word
        except ApiError as exc:
            warnings.append(str(exc))
    if not merged and warnings:
        raise ApiError(" ".join(warnings))
    return list(merged.values())[:limit], totals, warnings


def continuation_count(dictionaries: list[str], syllable: str, filters: Filters, dueum: bool) -> tuple[int, list[str]]:
    variants = get_dueum_variants(syllable) if dueum else [syllable]
    total_count = 0
    warnings = []
    for variant in variants:
        for dictionary in dictionaries:
            try:
                # 첫 항목이 한 글자 등의 필터에 걸려도 오판하지 않도록 한 묶음을 확인한다.
                words, total = fetch_dictionary(dictionary, variant, 1, API_PAGE_SIZE, filters)
                if words:
                    # 필터를 통과한 항목이 확인되면 원 API의 시작 일치 결과 수를 표시한다.
                    total_count += total
            except ApiError as exc:
                warnings.append(str(exc))
    return total_count, list(dict.fromkeys(warnings))


def paged_search(dictionaries: list[str], query: str, filters: Filters, page: int) -> tuple[list[dict], int, list[str]]:
    """화면에 필요한 한 페이지만 가져와 첫 응답 시간을 제한한다."""
    merged: dict[str, dict] = {}
    total, warnings = 0, []
    needed = page * PAGE_SIZE
    for dictionary in dictionaries:
        try:
            words: list[dict] = []
            api_start, dictionary_total = 1, 0
            # 한 글자·전문어 등이 앞쪽을 채워 모두 걸러지는 경우 다음 묶음도 확인한다.
            while len(words) < needed and (not dictionary_total or api_start <= dictionary_total) and api_start <= 500:
                batch, dictionary_total = fetch_dictionary(dictionary, query, api_start, API_PAGE_SIZE, filters)
                words.extend(batch)
                api_start += API_PAGE_SIZE
            words = words[(page - 1) * PAGE_SIZE:needed]
            total += dictionary_total
            for word in words:
                current = merged.get(word["word"])
                if current:
                    current["dictionary_codes"].extend(code for code in word["dictionary_codes"] if code not in current["dictionary_codes"])
                else:
                    merged[word["word"]] = word
        except ApiError as exc:
            warnings.append(str(exc))
    if not merged and warnings:
        raise ApiError(" ".join(warnings))
    return list(merged.values()), total, warnings


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", dictionaries={key: bool(os.getenv(value["key_env"])) for key, value in DICTIONARIES.items()})


@app.get("/api/search")
def search():
    try:
        query = validate_query(request.args.get("query", ""))
        dictionaries = selected_dictionaries(request.args.get("dictionary", "stdict"))
        mode = request.args.get("mode", "all")
        if mode not in {"all", "words", "one-shot"}:
            raise ValueError("올바른 검색 유형을 선택해 주세요.")
        page = max(1, int(request.args.get("page", 1)))
        filters = Filters(**{name: as_bool(name) for name in Filters.__annotations__})
        dueum = as_bool("dueum", True)
        candidates, raw_total, warnings = paged_search(dictionaries, query, filters, page)
        syllables = {last_hangul_syllable(word["word"]) for word in candidates}
        counts: dict[str, tuple[int, list[str]]] = {}
        # 서로 독립적인 끝 글자 조회를 병렬 처리해 순차 네트워크 대기를 없앤다.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(syllables)))) as executor:
            futures = {executor.submit(continuation_count, dictionaries, syllable, filters, dueum): syllable for syllable in syllables}
            for future in as_completed(futures):
                counts[futures[future]] = future.result()
        analysed = []
        for word in candidates:
            last = last_hangul_syllable(word["word"])
            count, notes = counts.get(last, (0, []))
            warnings.extend(notes)
            word.update(last_syllable=last, next_word_count=count, is_one_shot=count == 0,
                        dictionary="두 사전 공통" if len(word["dictionary_codes"]) == 2 else DICTIONARIES[word["dictionary_codes"][0]]["name"])
            analysed.append(word)
        one_shot_count = sum(word["is_one_shot"] for word in analysed)
        visible = [w for w in analysed if mode != "one-shot" or w["is_one_shot"]]
        return jsonify(query=query, dictionary=request.args.get("dictionary", "stdict"), dictionary_name=" + ".join(DICTIONARIES[x]["name"] for x in dictionaries),
                       total=raw_total, api_total=raw_total, one_shot_count=one_shot_count,
                       page=page, page_size=PAGE_SIZE, has_more=page * PAGE_SIZE < raw_total,
                       analysed_count=len(analysed), words=visible, warnings=list(dict.fromkeys(warnings)))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except ApiError as exc:
        return jsonify(error=str(exc)), 502


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")

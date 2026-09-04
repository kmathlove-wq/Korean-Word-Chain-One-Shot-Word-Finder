# Project Memory

- 실행: `python3`는 이 PC에서 깨진 별칭. `py -3 -m venv .venv` 만든 뒤 항상 `.venv\Scripts\python`으로 실행(Flask/requests/dotenv는 전역에 없음).
- 검사 3종: `.venv\Scripts\python -m unittest discover -s tests -v` / `... -m compileall -q app.py tests` / `node --check static/main.js`.
- 테스트는 모듈 전역 `app.cache`를 공유하므로 캐시가 결과에 영향 주는 새 테스트는 `setUp`에서 `app.cache._items.clear()` 필요.
- 한방단어 모드는 `gather_one_shot_words()`가 페이지 무관하게 전체 목록을 1회 수집·캐시하고 라우트가 슬라이스. 페이지 넘김을 다시 손대면 이 구조 유지.
- 두음 한방 판정은 `continuation_count()`에서만 역방향(`dueum_reverse_variants`)까지 확인. 단어 목록 경로(`paged_search_with_dueum`)는 건드리지 않는다.
- 우리말샘은 옛말의 옛한글을 사용자 지정 영역(PUA, 예: U+E451) 코드로 준다. 표준 글꼴에 그림이 없어 네모(□)로 보인다. 사용자 결정(2026-09-04): 표기는 그대로 두고 `main.js`의 `ARCHAIC_HANGUL` 정규식으로 찾아 카드에 안내 문구만 붙인다. 지우거나 변환하지 않는다.
- 첫 검색이 느린 원인(측정): 단어 목록은 2~3초면 오지만 카드마다 '이어갈 단어 수'를 세느라 국립국어원에 수십 번 물어봄. 캐시가 차면 0.3초.
- 두 단계 로딩: 가나다·짧은·긴 순은 `defer_counts=1` → `search()`가 `describe_words_without_counts()`로 단어만 먼저(`deferred=true`), 화면 `fillDeferredCounts()`가 `GET /api/continuations`로 숫자·한방 뱃지를 채움. `next`·`one-shot` 정렬과 `mode=one-shot`은 예전대로 한 번에.
- 끝 글자 병렬 조회는 `fast_continuation_counts()` 하나로 통일(= `analyse_words` 빠른 경로 + `/api/continuations` 공유). 작업자 수 `LOOKUP_WORKERS`(20), 연결은 공유 `_http = requests.Session()` 재사용.

# Project Memory

- 실행: `python3`는 이 PC에서 깨진 별칭. `py -3 -m venv .venv` 만든 뒤 항상 `.venv\Scripts\python`으로 실행(Flask/requests/dotenv는 전역에 없음).
- 검사 3종: `.venv\Scripts\python -m unittest discover -s tests -v` / `... -m compileall -q app.py tests` / `node --check static/main.js`.
- 테스트는 모듈 전역 `app.cache`를 공유하므로 캐시가 결과에 영향 주는 새 테스트는 `setUp`에서 `app.cache._items.clear()` 필요.
- 한방단어 모드는 `gather_one_shot_words()`가 페이지 무관하게 전체 목록을 1회 수집·캐시하고 라우트가 슬라이스. 페이지 넘김을 다시 손대면 이 구조 유지.
- 두음 한방 판정은 `continuation_count()`에서만 역방향(`dueum_reverse_variants`)까지 확인. 단어 목록 경로(`paged_search_with_dueum`)는 건드리지 않는다.

# 끝말잇기 한방단어 검색기 — CLAUDE.md

## 프로젝트 개요

국립국어원의 표준국어대사전과 우리말샘 공식 Open API를 사용하는 한국어 끝말잇기 보조 웹앱이다. 사용자가 입력한 한글로 시작하는 단어를 찾고, 마지막 유효 한글 음절로 이어갈 단어가 있는지 확인해 한방단어를 판정한다.

프런트엔드는 HTML/CSS/JavaScript, 백엔드는 Python/Flask/requests로 구성된다. Python 서버가 필요하므로 VS Code Live Server나 GitHub Pages만으로는 정상 동작하지 않는다.

## 파일 구조

```text
/
├── app.py                 # Flask 라우트, 사전 API, 캐시, 필터, 한방 판정
├── requirements.txt       # Flask, requests, python-dotenv
├── .env                   # 실제 API 키, Git 제외 대상
├── .env.example           # 키 이름만 담은 공개 예시
├── .gitignore             # .env, 가상환경, 캐시 제외
├── README.md              # 설치·실행·테스트·배포 안내
├── templates/
│   └── index.html         # 검색 폼, 로딩, 결과 카드, 안내 UI
├── static/
│   ├── style.css          # 반응형 디자인과 접근성 스타일
│   └── main.js            # 입력 검증, API 호출, 정렬, 렌더링, 복사
├── tests/
│   └── test_app.py        # 파서·검증·라우트·한방 판정 테스트
├── AGENTS.md              # Codex 및 자동화 에이전트 작업 지침
└── CLAUDE.md              # Claude용 프로젝트 지식
```

## 실행

```bash
python -m venv .venv
pip install -r requirements.txt
python app.py
```

접속 주소는 `http://127.0.0.1:5000`이다. `/api/health`에서 서버 상태와 각 사전 키의 설정 여부를 확인한다. Flask 서버를 재시작해야 변경된 `.env` 값이 반영된다.

## 환경 변수와 보안

```env
STDICT_API_KEY=표준국어대사전_키
OPENDICT_API_KEY=우리말샘_키
```

- 실제 키는 반드시 `.env`에만 저장한다.
- `.env.example`에는 실제 값이나 값의 일부를 넣지 않는다.
- `.env`는 `.gitignore`에서 제외하며 Git에 강제 추가하지 않는다.
- 로그, 테스트 출력, 문서, 채팅에 키를 표시하지 않는다.
- 배포 시 호스팅 서비스의 환경 변수/Secret 기능에 같은 이름으로 등록한다.

## API 라우트

| 라우트 | 역할 |
|---|---|
| `GET /` | Jinja 템플릿으로 메인 화면 렌더링 |
| `GET /api/health` | 서버 상태와 사전별 키 설정 여부 반환 |
| `GET /api/search` | 시작 단어 검색, 필터, 한방 판정, 페이지 응답 |

`/api/search`의 주요 매개변수는 `query`, `dictionary`, `mode`, `page`, `noun_only`, `include_proper`, `include_north`, `include_dialect`, `include_old`, `include_technical`, `include_single`, `dueum`이다. `dictionary` 값은 `stdict`, `opendict` 중 하나다. `mode` 값은 `all`, `words`, `one-shot` 중 하나다.

## 백엔드 핵심 규칙

- 공식 엔드포인트만 사용하며 네이버 사전 크롤링이나 가짜 데이터를 추가하지 않는다.
- 표준국어대사전은 `https://stdict.korean.go.kr/api/search.do`, 우리말샘은 `https://opendict.korean.go.kr/api/search`를 사용한다.
- JSON 응답을 우선 처리하고 JSON이 아니면 `xml.etree.ElementTree`로 XML을 파싱한다.
- 검색 방식은 `type_search=search`, `method=start`인 시작 일치 검색이다.
- 요청 제한 시간은 연결 10초/응답 20초이며 실패 시 한 번 재시도한다.
- 화면 페이지 크기는 24개, 공식 API 묶음 크기는 100개다.
- 필터로 앞쪽 결과가 모두 제거될 수 있으므로 `paged_search()`는 필요한 결과가 모일 때까지 최대 500개 범위에서 다음 묶음을 확인한다.
- 메모리 `TTLCache`의 기본 만료 시간은 30분이다. 서버 재시작 시 사라지며 프로세스 간 공유되지 않는다.
- 화면에서는 표준국어대사전 또는 우리말샘 중 하나만 선택해 검색한다.

## 한방단어 판정

- `last_hangul_syllable()`은 공백, 숫자, 괄호, 하이픈, 특수문자를 무시하고 마지막 `[가-힣]` 음절을 찾는다.
- `continuation_count()`는 마지막 음절로 시작하는 단어를 선택 사전에서 다시 검색한다.
- 첫 항목이 한 글자 제외 등의 필터에 걸리는 오판을 방지하기 위해 후속 검색은 최대 100개 묶음을 확인한다.
- 필터를 통과한 후속 단어가 하나라도 있으면 한방단어가 아니다.
- `dueum=true`이면 원래 음절과 `DUEUM_MAP` 변환 음절을 모두 검사한다. 어느 한쪽에라도 단어가 있으면 한방단어가 아니다.
- 현재 이어갈 단어 수는 필터 통과 항목이 확인된 API 시작 일치 총계를 사용한다. 두음 변형 결과 수에는 중복이 포함될 수 있다.

## 두음법칙

두음 변환은 프런트엔드에 복제하지 않고 `app.py`의 `DUEUM_MAP`과 `get_dueum_variants()`에서만 관리한다. 현재 지원 변환은 `녀→여`, `뇨→요`, `뉴→유`, `니→이`, `랴→야`, `려→여`, `례→예`, `료→요`, `류→유`, `리→이`, `라→나`, `래→내`, `로→노`, `뢰→뇌`, `루→누`, `르→느`다.

## 프런트엔드 규칙

- 폼 제출 또는 Enter 입력 시에만 검색한다.
- 클라이언트에서도 완성형 한글 1~20자인지 검사하지만 서버 검증을 항상 유지한다.
- 사용자/API 문자열은 `escapeHtml()`을 거쳐 렌더링한다.
- 로딩·메시지·결과 영역은 `hidden` 속성으로 제어하며 `[hidden]{display:none!important}` 규칙을 유지한다.
- 정렬은 현재 브라우저에 불러온 결과를 대상으로 한다.
- 모바일에서 상세 설정은 `details` 요소로 접을 수 있어야 한다.

## 테스트

```bash
python -m unittest discover -s tests -v
python -m compileall -q app.py tests
node --check static/main.js
```

실제 키가 있을 때는 `기`, `가`, `트`, `슘`처럼 결과 규모가 다른 음절을 검색한다. 특히 `한 글자 포함`을 끈 상태에서도 `가`나 `트`로 이어갈 단어가 존재하면 한방단어로 나오지 않아야 한다.

## 변경 절차와 작업 규칙

1. 작업 전 `git status --short`로 기존 사용자 변경을 확인한다.
2. 관련 코드와 테스트를 읽고 기존 기능과 디자인을 보존한다.
3. API 키나 `.env` 내용을 출력하지 않는다.
4. 판정 로직 변경에는 재현 가능한 회귀 테스트를 추가한다.
5. 단위 테스트, Python/JavaScript 구문 검사, `git diff --check`를 실행한다.
6. 실제 API 검사는 키가 설정된 경우 최소 호출로 수행한다.
7. 변경 파일, 검증 결과, 남은 제한을 보고한다.

- 사용자 요청 없이 기존 변경을 되돌리지 않는다.
- 공식 API 대신 크롤링, 비공식 프록시, 가짜 데이터를 추가하지 않는다.
- API 필드는 공식 문서 또는 실제 응답으로 확인하며 추측하지 않는다.
- 요청받지 않은 커밋이나 push는 수행하지 않는다.
- 동작이나 실행 방법이 바뀌면 README, AGENTS.md, CLAUDE.md를 함께 갱신한다.
- 두 에이전트 문서는 각각 200줄 이하로 유지한다.

## 배포

- GitHub Pages는 Flask를 실행하지 못한다. GitHub는 소스 저장소로 사용하고 Render, Railway, PythonAnywhere 등 Python WSGI 호스팅을 연결한다.
- 운영 환경에서는 Flask 개발 서버를 사용하지 않고 Gunicorn 같은 WSGI 서버를 사용한다.
- API 신청 URL은 로컬 개발 시 `http://127.0.0.1:5000`, 배포 후 실제 HTTPS 서비스 주소로 갱신한다.
- 운영 전 요청 빈도 제한, 공유 캐시, 오류 로깅, HTTPS를 준비한다.

## 알려진 제한

- 국립국어원 API 응답 속도와 일일 호출 제한에 영향을 받는다.
- API 분류 필드가 일정하지 않아 일부 필터가 사전 웹사이트의 상세 검색과 완전히 같지 않을 수 있다.
- 두음 변형 결과 수는 중복 제거된 정확한 합계가 아닐 수 있으나 한방 여부는 하나라도 존재하는지를 기준으로 한다.
- 한방 판정은 선택한 사전과 필터 기준이며 실제 게임 규칙을 보장하지 않는다.

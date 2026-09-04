# 끝말잇기 한방단어 검색기

국립국어원의 **표준국어대사전** 및 **우리말샘 공식 Open API**로 입력한 글자로 시작하는 단어와 끝말잇기 한방단어를 찾는 Flask 웹 애플리케이션입니다. 사전 데이터나 가짜 결과를 저장하지 않으며, API 키는 서버 환경 변수에서만 읽습니다.

## 주요 기능

- 표준국어대사전 또는 우리말샘 단일 사전 검색
- 뜻, 품사, 사전 출처, 상세 링크 표시 및 단어 복사
- 마지막 유효 한글 음절 추출과 이어갈 단어 수 분석
- 두음법칙 변환음(정방향 + 역방향 원래 소리)을 함께 확인하는 한방단어 판정
- 명사, 고유명사, 북한어, 방언, 옛말, 전문어, 한 글자 필터
- 동음이의어는 한 카드에 서로 다른 뜻을 최대 3개까지 묶어 표시
- 24개 단위 결과 페이지와 다섯 가지 정렬(정렬을 바꾸면 서버에 새로 요청)
- 두 단계 로딩: 목록을 먼저 보여주고 "이어갈 단어 수"·한방단어 여부는 `/api/continuations`로 뒤이어 채움(한방단어 모드 포함)
- 옛말의 옛한글(사용자 지정 영역 코드)이 든 낱말에는 "네모로 보일 수 있다"는 안내 표시
- 사전·검색어·필터·API 페이지별 30분 메모리 캐시
- 요청 제한 시간, 일부 사전 실패 경고, 입력값 및 인증키 오류 처리
- 모바일/태블릿/PC 대응 및 키보드 접근성

## 폴더 구조

```text
.
├── app.py                 # Flask API, 사전 클라이언트, 캐시, 판정 로직
├── requirements.txt       # Python 패키지
├── .env.example           # 환경 변수 예시
├── .gitignore             # 비밀 키와 가상환경 제외
├── README.md
├── templates/index.html   # 화면 구조
├── static/style.css       # 반응형 디자인
├── static/main.js         # 검색, 정렬, 결과 렌더링
└── tests/test_app.py      # 키 없이 실행 가능한 자동 검사
```

## 설치와 실행

Python 3.10 이상을 권장합니다.

```bash
python -m venv .venv
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\activate
```

패키지를 설치합니다.

```bash
pip install -r requirements.txt
```

`.env.example`을 `.env`로 복사한 뒤 국립국어원에서 발급받은 키를 넣습니다.

```env
STDICT_API_KEY=발급받은_표준국어대사전_키
OPENDICT_API_KEY=발급받은_우리말샘_키
```

- 표준국어대사전: <https://stdict.korean.go.kr/openapi/openApiInfo.do>
- 우리말샘: <https://opendict.korean.go.kr/service/openApiInfo>

두 서비스의 키가 서로 다를 수 있습니다. 한 사전만 이용할 경우 해당 키만 설정해도 됩니다. **실제 `.env`와 API 키를 GitHub에 커밋하지 마세요.** `.gitignore`에 `.env`가 등록되어 있습니다.

```bash
python app.py
```

브라우저에서 <http://127.0.0.1:5000>에 접속합니다. 상태 확인 주소는 <http://127.0.0.1:5000/api/health>입니다. 운영 배포에서는 Flask 개발 서버 대신 WSGI 서버를 사용하고, `FLASK_DEBUG`를 켜지 마세요.

## 테스트

API 키가 없어도 파서, 입력 검증, 두음법칙, 라우트 계약을 검사할 수 있습니다.

```bash
python -m unittest discover -s tests -v
python -m compileall app.py tests
```

실제 연동은 `.env` 설정 후 다음처럼 확인합니다.

```bash
curl "http://127.0.0.1:5000/api/search?query=기&dictionary=stdict&mode=all&noun_only=true&dueum=true&page=1"
curl "http://127.0.0.1:5000/api/search?query=기&dictionary=stdict&mode=words&sort=alphabet&defer_counts=1"
curl "http://127.0.0.1:5000/api/continuations?dictionary=stdict&syllables=강,물,릉&dueum=true"
```

## 동작과 제한

검색은 공식 API의 시작 일치 검색을 사용하고, JSON 응답을 우선 처리하며 XML 응답도 안전하게 파싱합니다. 한 묶음은 100개이고 필요한 페이지가 채워질 때까지 최대 10묶음(약 1000개) 범위에서 다음 묶음을 확인하며, 화면에는 24개씩 보여 줍니다. 같은 끝 글자의 조회는 캐시에서 재사용하고, 국립국어원 서버 연결은 공유 세션으로 재사용하며 독립적인 끝 글자 조회는 최대 36개까지 동시에 처리합니다. 메모리 캐시는 서버 재시작 시 초기화되며 다중 프로세스 사이에서는 공유되지 않습니다.

검색은 두 단계로 나눠 응답합니다. 먼저 목록만 돌려주고(`deferred: true`), 화면이 `/api/continuations`로 끝 글자들의 이어갈 단어 수와 한방단어 여부를 이어서 채웁니다. 그동안 각 카드의 이어갈 단어 수는 "확인 중…"으로 보입니다. 한방단어 모드도 두 단계로, 1단계에서 희귀 끝글자 한방단어를 먼저 보여 주고 나머지 후보는 "한방단어인지 확인 중…"으로 표시했다가 확인되면 한방이 아닌 카드를 지웁니다. `이어갈 단어가 적은 순`·`한방단어 우선` 정렬만 예전처럼 한 번에 계산합니다.

우리말샘은 옛말의 옛한글을 사용자 지정 영역(PUA) 코드로 제공합니다. 표준 글꼴에는 그 자리에 글자 그림이 없어 네모(□)로 보이며, 화면은 표기를 그대로 두고 "네모로 보일 수 있다"는 안내만 덧붙입니다.

한방단어 모드는 페이지 1에서 한방단어 전체 목록을 한 번에 모아 캐시하고, 다음 페이지는 그 목록을 잘라서 보여 줍니다(빈 페이지 반복 없음). 정렬을 바꾸면 브라우저가 서버에 새로 요청합니다. 주소창에서 필터 값 없이 `/api/search`를 불러도 화면 기본값(한 글자 포함만 꺼짐)과 같은 결과가 나옵니다.

API의 분류 필드는 표제어마다 비어 있거나 표현이 다를 수 있어 일부 필터 결과가 사전 웹사이트의 상세 검색과 완전히 같지 않을 수 있습니다. 화면에서는 표준국어대사전 또는 우리말샘 중 하나를 선택해 단일 사전 기준으로 검색합니다. 원 API 총계는 응답의 `api_total`에 별도로 둡니다. 한방단어는 선택한 사전·필터·두음법칙 설정 안에서만 판정되므로 실제 게임 규칙과 다를 수 있습니다.

## 오류 해결

- `API 키가 설정되지 않았습니다`: `.env` 이름과 키 값, 실행 위치를 확인합니다.
- 인증 오류: 해당 사전에서 발급된 키인지와 사용 승인을 확인합니다.
- 연결/시간 초과: 인터넷 연결, 국립국어원 서비스 상태를 확인한 뒤 다시 시도합니다.
- 결과 없음: 전문어 등 포함 필터를 켜거나 다른 사전을 선택합니다.
- `ModuleNotFoundError`: 가상환경을 활성화하고 `pip install -r requirements.txt`를 다시 실행합니다.

## GitHub 및 Render 배포

이 프로젝트는 Flask 서버와 비밀 API 키가 필요하므로 **GitHub Pages만으로는 배포할 수 없습니다.** GitHub에는 소스 코드를 올리고, Render의 Web Service에 저장소를 연결합니다.

1. GitHub에서 새 저장소를 만든 뒤 이 프로젝트를 `main` 브랜치로 push합니다. `.env` 파일은 절대 추가하지 않습니다.
2. [Render Dashboard](https://dashboard.render.com/)에서 **New + → Web Service**를 선택하고 GitHub 저장소를 연결합니다.
3. `render.yaml`을 인식시키거나 다음 값을 직접 입력합니다.

   ```text
   Language: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: gunicorn app:app
   Health Check Path: /api/health
   ```

4. Render 서비스의 **Environment**에서 다음 두 변수를 추가합니다. 실제 값은 로컬 `.env`의 키를 복사해 넣고, GitHub에는 올리지 않습니다.

   ```text
   STDICT_API_KEY=발급받은_표준국어대사전_키
   OPENDICT_API_KEY=발급받은_우리말샘_키
   ```

5. 배포가 끝나면 Render가 제공하는 `https://...onrender.com` 주소에서 `/api/health`를 열어 사용할 사전 키 값이 `true`인지 확인합니다.
6. 국립국어원 Open API의 사용 URL을 새 Render 주소로 변경하거나 추가합니다.

Render는 연결한 GitHub 브랜치에 push할 때마다 자동으로 다시 배포합니다. 운영 환경에서는 HTTPS, 요청 빈도 제한, 공유 캐시(Redis 등), 로그에서 검색어/API 키 마스킹을 추가하는 것이 좋습니다. 사전 데이터의 출처와 이용 조건은 각 공식 사이트의 최신 지침을 따르세요.

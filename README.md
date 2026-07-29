# Gitcord

GitHub 저장소를 추적해 **커밋 · PR · 이슈 · CI 결과**를 디스코드 채널로 보내는 봇입니다.
저장소마다 받을 알림 종류를 따로 고를 수 있습니다.

```
/watch add repo:owner/name
```

<sub>Python 3.11+ · discord.py 2.x · 외부 서비스 의존 없음(GitHub API 만 사용)</sub>

## 특징

- **웹훅 설정이 필요 없습니다.** 저장소 설정을 건드릴 권한이 없어도, 읽기 권한만 있으면
  추적할 수 있습니다. 공인 IP 나 포트 개방도 필요 없어서 무료 봇 호스팅에 그대로 올라갑니다.
- **저장소마다 알림 종류를 따로 고릅니다.** 어떤 저장소는 커밋만, 어떤 저장소는 CI 실패만.
- **채널을 나눠 받을 수 있습니다.** 같은 저장소를 `#dev` 와 `#ci` 에 다른 설정으로 구독해도 됩니다.
- **API 호출을 아낍니다.** ETag 조건부 요청을 써서 변경이 없으면 호출 한도를 소모하지 않고,
  커서를 저장소 단위로 공유해 서버 여러 개가 같은 저장소를 봐도 GitHub 호출은 한 번입니다.
- **메모리 128MB 안에서 돕니다.** 무료 호스팅 상한을 전제로 만들었습니다 (실측 약 53MB).

---

## 준비물

### 1. 디스코드 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** 탭 → **Reset Token** → 나온 토큰을 복사 (`DISCORD_TOKEN`)
3. **Installation** 탭 → Scopes 에 `bot` 과 `applications.commands` 체크
4. Bot Permissions 에 **Send Messages**, **Embed Links** 체크
5. 생성된 초대 링크로 서버에 추가

특권 인텐트(Message Content, Server Members)는 **필요 없습니다.** 슬래시 커맨드만 씁니다.

### 2. GitHub 토큰 (권장)

없어도 돌아가지만 API 가 **시간당 60회**로 제한되고 비공개 저장소를 추적할 수 없습니다.

[Settings → Developer settings → Personal access tokens](https://github.com/settings/tokens) 에서 발급합니다.
- 공개 저장소만 추적 → 권한 없는 토큰으로 충분 (시간당 5,000회로 올라감)
- 비공개 저장소도 추적 → 해당 저장소의 `Contents: Read`

---

## 로컬 실행

```bash
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env        # macOS/Linux: cp .env.example .env
# .env 를 열어 DISCORD_TOKEN 과 GITHUB_TOKEN 을 채웁니다

python main.py
```

개발 중에는 `.env` 의 `GUILD_ID` 에 테스트 서버 ID 를 넣으세요. 슬래시 커맨드가
그 서버에 **즉시** 등록됩니다. 비워두면 전역 등록이라 디스코드 반영까지 최대 1시간
걸립니다.

테스트:

```bash
python tests/test_core.py
```

---

## Dishost 배포

1. 새 봇 생성 → **Python** 선택
2. 파일 업로드 (`.venv`, `data/`, `.env` 는 제외 — `.gitignore` 참고)
3. **환경변수** 탭에 `DISCORD_TOKEN`, `GITHUB_TOKEN` 등록 (`.env` 파일 대신 써도 됩니다)
4. 실행 파일: `main.py`

### 128MB 메모리 안에서 돌리기

Dishost 무료 플랜은 RAM 128MB 입니다. 이 봇은 그 상한을 전제로 만들어졌습니다.

- 멤버 · 메시지 캐시를 모두 끄고 최소 인텐트(`guilds`)만 씁니다
- `aiosqlite` 없이 stdlib `sqlite3` 만 씁니다
- 한 사이클에 저장소당 최대 15건까지만 전송합니다

의존성을 추가할 때는 **패널의 메모리 사용량을 반드시 다시 확인하세요.**

---

## 명령어

| 명령 | 권한 | 설명 |
|---|---|---|
| `/watch add repo channel? events?` | 서버 관리 | 저장소 구독 추가 |
| `/watch remove repo channel?` | 서버 관리 | 구독 해제 (채널 생략 시 서버 전체) |
| `/watch list` | 누구나 | 이 서버의 구독 목록 |
| `/watch events repo events` | 서버 관리 | 저장소별 알림 종류 변경 |
| `/repo owner/name` | 누구나 | 저장소 정보 조회 |
| `/ping` · `/gitcord` | 누구나 | 상태 · 도움말 |

### 알림 종류

`events:` 에 쉼표로 넣습니다. 전체는 `all`.

| 값 | 내용 | 기본 |
|---|---|:---:|
| `push` | 커밋 푸시 | ✅ |
| `pr` | PR 열림 · 머지 · 닫힘 · 리뷰 · 댓글 | ✅ |
| `issue` | 이슈 열림 · 닫힘 · 댓글 | ✅ |
| `ci` | GitHub Actions 성공 · 실패 | ✅ |
| `release` | 릴리스 발행 | ✅ |
| `branch` | 브랜치 · 태그 생성/삭제 | |
| `star` | 스타 · 포크 | |

```
/watch add repo:psf/requests events:push,ci
/watch events repo:psf/requests events:all
```

---

## 동작 방식

**웹훅이 아니라 폴링입니다.** Dishost 가 인바운드 HTTP 포트를 열어주는지 확실하지
않아 설계 단계에서 웹훅을 뺐습니다. 대신 GitHub API 를 주기적으로 조회합니다.

- `GET /repos/{repo}/events` 로 push · PR · 이슈 · 릴리스 · 브랜치를 **한 번에** 받습니다
- **ETag 조건부 요청**을 씁니다. 변경이 없으면 304 가 오고, 304 는 API 호출 한도를
  소모하지 않습니다. 저장소 하나를 90초마다 찔러도 실제 소모는 활동이 있을 때뿐입니다
- GitHub 이 `X-Poll-Interval` 헤더로 더 긴 주기를 권하면 그쪽을 따릅니다
- GitHub Actions 는 활동 이벤트에 포함되지 않아, `ci` 를 구독한 저장소만 따로 조회합니다
- 커서는 저장소 단위입니다. 서버 3개가 같은 저장소를 구독해도 GitHub 호출은 1회입니다

`POLL_INTERVAL_SECONDS` 로 주기를 바꿀 수 있습니다(최소 30초, 기본 90초).

### 구독 직후 동작

첫 폴링에서는 **아무것도 보내지 않고 기준점만 잡습니다.** 안 그러면 구독하자마자
과거 30건이 채널에 쏟아집니다. 다음 활동부터 알림이 갑니다.

---

## 알려진 제한

- **인라인 리뷰 코멘트**(`PullRequestReviewCommentEvent`)는 보내지 않습니다. 코드 한
  줄마다 알림이 가면 채널이 무너집니다. 리뷰 승인 · 변경요청 · 리뷰 본문은 보냅니다.
- **라벨 부착 · 담당자 변경** 같은 잡음성 액션은 무시합니다.
- 활동이 폭주하면 한 사이클에 저장소당 **15건까지만** 보내고 나머지는 버립니다(로그에 기록).
- 워크플로 실행이 순서를 바꿔 끝나도 놓치지 않지만, 200건을 넘어서는 아주 오래된
  실행이 뒤늦게 완료되면 중복 알림이 갈 수 있습니다.
- GitHub 이 비공개 저장소에 대해 접근 권한이 없을 때도 404 를 돌려주기 때문에,
  404 가 나도 구독을 자동으로 지우지 않고 1시간 뒤 재시도합니다. `/watch list` 와
  로그를 확인하세요.

---

## 환경변수

| 이름 | 필수 | 기본 | 설명 |
|---|:---:|---|---|
| `DISCORD_TOKEN` | ✅ | | 봇 토큰 |
| `GITHUB_TOKEN` | | | 없으면 시간당 60회 제한 · 비공개 저장소 불가 |
| `GUILD_ID` | | | 개발용 테스트 서버 ID (커맨드 즉시 등록) |
| `POLL_INTERVAL_SECONDS` | | `90` | 폴링 주기(초), 최소 30 |
| `DATA_DIR` | | `data` | SQLite 파일 위치 |
| `GITCORD_SECRET_KEY` | | | AI 요약용 (아직 미사용) |

---

## 프로젝트 구조

```
main.py                    엔트리포인트
gitcord/
  config.py                환경변수 로딩·검증
  db.py                    SQLite (stdlib sqlite3 + to_thread)
  bot.py                   봇 본체·알림 전송·에러 처리
  categories.py            알림 카테고리 정의 (embeds·watch·watcher 공용)
  embeds.py                GitHub 이벤트 → 디스코드 임베드
  github/client.py         GitHub REST 클라이언트 (ETag·rate limit·백오프)
  cogs/
    general.py             /ping, /gitcord
    repo.py                /repo
    watch.py               /watch add|remove|list|events
    watcher.py             폴링 엔진
tests/test_core.py         테스트 (pytest 불필요)
```

### 개발 메모

GitHub 저장소 이벤트 API 에는 문서만 보고는 알기 어려운 함정이 몇 개 있습니다.
같은 실수를 반복하지 않도록 테스트에 ★ 표시로 고정해뒀습니다.

- **PushEvent 페이로드에 `commits` 가 없습니다.** `before`/`head`/`push_id`/`ref`/
  `repository_id` 뿐이라, 커밋 목록은 `compare` API 로 따로 받아야 합니다.
- **이벤트 id 가 단조 증가하지 않습니다.** Push·Create·Delete 는 `15,9xx,xxx,xxx` 대역을,
  PR·이슈·스타는 `12,4xx,xxx,xxx` 대역을 씁니다. `max(id)` 를 커서로 쓰면 커밋이 한 번
  푸시된 뒤로 PR·이슈 알림이 전부 걸러집니다. 그래서 커서가 "최근 본 id 집합"입니다.
- **action 이름이 웹훅과 다릅니다.** 리뷰는 `submitted` 가 아니라 `created`, PR 머지는
  `closed`+`merged:true` 가 아니라 action 자체가 `merged` 로 옵니다.
- aiohttp 응답 헤더를 `dict()` 로 바꾸면 대소문자 무시 조회가 깨집니다. GitHub 은
  `ETag` 를 대문자로 보내므로 그대로 두면 조건부 요청이 통째로 무력화됩니다.

## 진행 상황

- [x] 뼈대 · 설정 · SQLite
- [x] GitHub 클라이언트 (ETag · rate limit · 백오프)
- [x] 임베드 · 폴링 엔진
- [x] `/watch` 계열 (저장소별 이벤트 설정)
- [x] GitHub Actions 폴링
- [ ] **AI 커밋 요약** — 서버별로 Claude · OpenAI · Gemini API 키를 등록하면 그 키로
      커밋 diff 를 읽어 한국어 요약을 붙입니다. 키는 암호화 저장하고 사용량 상한을 둡니다.

---

## 라이선스

아직 정하지 않았습니다. 라이선스 파일이 없으면 기본적으로 **모든 권리가 저작자에게
유보**되며, 다른 사람이 법적으로 사용·수정·재배포할 수 없습니다. 공개해서 쓰이길
원한다면 `LICENSE` 파일을 추가하세요 (MIT 가 무난합니다).

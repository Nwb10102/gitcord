# Gitcord

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

GitHub 저장소를 추적해 **커밋 · PR · 이슈 · CI 결과**를 디스코드 채널로 보내는 봇입니다.
저장소마다 받을 알림 종류를 따로 고를 수 있습니다.

```
/watch add repo:owner/name
```

<sub>Python 3.11+ · discord.py 2.x · 외부 서비스 의존 없음(GitHub API 만 사용) · MIT License</sub>

## 특징

- **웹훅 설정이 필요 없습니다.** 저장소 설정을 건드릴 권한이 없어도, 읽기 권한만 있으면
  추적할 수 있습니다. 공인 IP 나 포트 개방도 필요 없어서 무료 봇 호스팅에 그대로 올라갑니다.
- **저장소마다 알림 종류를 따로 고릅니다.** 어떤 저장소는 커밋만, 어떤 저장소는 CI 실패만.
- **채널을 나눠 받을 수 있습니다.** 같은 저장소를 `#dev` 와 `#ci` 에 다른 설정으로 구독해도 됩니다.
- **API 호출을 아낍니다.** ETag 조건부 요청을 써서 변경이 없으면 호출 한도를 소모하지 않고,
  커서를 저장소 단위로 공유해 서버 여러 개가 같은 저장소를 봐도 GitHub 호출은 한 번입니다.
- **메모리 128MB 안에서 돕니다.** 무료 호스팅 상한을 전제로 만들었습니다 (실측 약 53MB).

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

---

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

이 프로젝트는 [MIT License](LICENSE)를 따르는 오픈소스 소프트웨어입니다. 자유롭게 사용, 수정, 배포하실 수 있습니다.

# f1-race-engineer

F1 25 텔레메트리 기반 한국어 AI 레이스 엔지니어. 자세한 내용은 `docs/design/` 참고.

## 빠른 시작

```sh
uv sync                              # 의존성·.venv 준비
uv run python tools/fetch_assets.py  # 무전 시작 효과음 받기(한 번만)
uv run f1re serve                    # UDP 20777 수신 + 웹 8093
```

태블릿에서 `http://<서버>:8093/`(대시보드), 게임 PC에서
`http://<서버>:8093/radio`(무전)를 열고 **무전 연결** 버튼을 누른다.
홈서버 서비스 등록·방화벽·게임 텔레메트리 설정은
[`deploy/README.md`](deploy/README.md)를 따른다.

## 실행

```sh
uv run f1re serve
```

UDP 20777(설정값)로 게임 패킷을 받으면서 같은 프로세스가 대시보드를 띄운다.
브라우저에서 `http://<서버>:8093/`을 연다. 웹 주소·포트는 `config/default.toml`의
`[web]`에 있고, `--web-host`/`--web-port`로 덮어쓸 수 있다(`--no-web`은 끔).
화면은 서버 API를 읽기만 하며, 색은 `/api/thresholds`의 표만 쓴다.

무전은 게임 PC 브라우저에서 `http://<서버>:8093/radio`를 열고 **무전 연결** 버튼을
한 번 누르면 들린다(브라우저 자동 재생 정책). 연결이 끊기면 페이지가 스스로 다시
연결하고, 연결 상태와 마지막 무전 자막을 크게 보여 준다.

## 레이스 엔지니어 켜기/끄기

웹 서버는 항상 뜨고, 게임 중에만 필요한 기능(UDP 수신·분석·무전·TTS·DB 기록)은
**엔진**으로 켜고 끈다. 주말에만 게임할 때 서버를 늘 메모리·CPU를 쓰게 두지
않기 위한 것이다. 마지막 상태는 `data/engine-state.json`에 남고, **파일이 없으면
(최초 실행) OFF로 시작**한다. 서버를 재시작해도 사용자가 마지막으로 고른
ON/OFF를 그대로 이어 간다(서비스 종료는 "사용자가 끈 것"으로 보지 않는다).

- 대시보드 우측 아래 **톱니바퀴 버튼** → **레이스 엔지니어** 토글
- `uv run f1re serve --engine on` / `--engine off` — 시작 상태 덮어쓰기
  (덮어쓴 값도 파일에 남아 다음 실행에 유지된다)
- `GET /api/engine` → `{"state": "on"|"off"}`,
  `POST /api/engine {"state": "on"|"off"}` → 전환 후 상태
- `GET /health` → `{"status": "ok", "engine": "on"|"off"}` (항상 200)

OFF일 때는 UDP 포트를 닫고 백그라운드 루프·TTS 호출·DB 쓰기를 모두 멈추며,
메모리에 있던 상태 저장소·무전 큐·음성 캐시를 해제한다(디스크의 음성 캐시
파일과 DB는 남는다). 화면(`/`·`/radio`)·정적 파일·`/health`·`/api/engine`만
동작하고, 그 밖의 API와 WebSocket은 503 `{"engine": "off"}`로 답한다.


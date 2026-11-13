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


# f1-race-engineer

F1 25 텔레메트리 기반 한국어 AI 레이스 엔지니어. 자세한 내용은 `docs/design/` 참고.

## 실행

```sh
uv run f1re serve
```

UDP 20777(설정값)로 게임 패킷을 받으면서 같은 프로세스가 대시보드를 띄운다.
브라우저에서 `http://<서버>:8080/`을 연다. 웹 주소·포트는 `config/default.toml`의
`[web]`에 있고, `--web-host`/`--web-port`로 덮어쓸 수 있다(`--no-web`은 끔).
화면은 서버 API를 읽기만 하며, 색은 `/api/thresholds`의 표만 쓴다.


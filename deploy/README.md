# 홈서버 배포

F1 25 게임 PC가 보내는 UDP 텔레메트리를 홈서버(리눅스)에서 받아, 태블릿
대시보드와 게임 PC 무전 페이지를 같은 서버가 제공한다. 이 문서는 서버에 한 번
설치하는 순서와 게임 쪽 설정을 다룬다.

- **서비스 등록·시작, 방화벽 설정은 서버에 로그인한 사용자가 직접 실행한다.**
  이 레포에는 서비스 파일과 문서만 있다.
- 기준 경로: `/home/golony/dev/repo/f1-race-engineer`
- 기준 사용자: `golony` — 다르면 아래 경로와 `deploy/f1re.service`의 사용자명 부분을 바꾼다.
- 포트: **UDP 20777**(게임 → 서버, 외부에 연다), **TCP 8093**(웹, 서버 안 `127.0.0.1`에서만 받는다)
- 웹 접속: 태블릿·게임 PC는 같은 서버의 **nginx**(HTTPS)를 거쳐 들어온다. nginx가
  `127.0.0.1:8093`으로 넘긴다.

```
[게임 PC] F1 25 ──UDP 20777──▶ [홈서버] f1re serve (웹 127.0.0.1:8093)
                                        ▲
                               [홈서버] nginx (HTTPS) ◀── [태블릿 대시보드]
                                                     ◀── [게임 PC /radio]
```

## 1. 준비물

- 리눅스 서버(설계 기준 i5-12500T·32GB·GPU 없음), 게임 PC와 같은 집 네트워크
- Python 3.14 이상 — `uv`가 프로젝트에 맞는 파이썬을 받아 준다(따로 설치하지 않아도 된다)
- `uv` — 없으면 [공식 설치 스크립트](https://docs.astral.sh/uv/getting-started/installation/)로 설치한다.

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  command -v uv        # 예: /home/golony/.local/bin/uv
  ```

- 공용 시크릿 설정 — Qwen TTS 키를 읽는 데 쓴다(4장). 서버 사용자의 홈에
  `~/.claude/bin/claude_secrets.py`와 `~/.claude/skills/qwen-key/get-qwen-key.py`가
  있어야 하고, 시크릿 `qwen` 항목이 등록되어 있어야 한다.
  (사용자 공통 설정 레포 `golony6449/golony-agent-support`의 설치 절차를 따른다.)

## 2. 체크아웃·설치·효과음

```sh
mkdir -p /home/golony/dev/repo/f1-race-engineer
git clone https://github.com/golony6449/f1-race-engineer.git \
  /home/golony/dev/repo/f1-race-engineer
cd /home/golony/dev/repo/f1-race-engineer
uv sync
```

`uv sync`가 `.venv`를 만들고 `f1re`(서버)와 `f1re-demo`(데모 송신기) 명령을 설치한다.
`config/default.toml`·`assets/` 같은 상대 경로는 **이 체크아웃(소스 트리)을 기준으로**
찾으므로, 서비스는 반드시 이 폴더에서 실행한다(`f1re.service`의 `WorkingDirectory`).

무전 시작 효과음은 저작권 때문에 레포에 없다. 한 번 받아 둔다:

```sh
uv run python tools/fetch_assets.py
# 저장: /home/golony/dev/repo/f1-race-engineer/assets/radio-intro.wav
```

이미 있으면 건너뛴다(`--force`면 다시 받는다). 파일이 없어도 무전 음성은 나오고,
시작 효과음만 짧은 클릭음으로 대신한다.

## 3. 설정과 포트

기본값은 `config/default.toml`에 있다. 보통은 그대로 쓴다.

| 항목 | 설정 키 | 기본값 | 뜻 |
|---|---|---|---|
| 게임 수신 | `[global] udp_port` | `20777` | 게임 PC가 보내는 UDP 포트 |
| 웹 주소 | `[web] host` | `"0.0.0.0"` | 웹을 받을 주소. 서비스는 `--web-host 127.0.0.1`로 덮어쓴다 |
| 웹 포트 | `[web] port` | `8093` | 대시보드·무전 페이지 포트 |

명령줄로 덮어쓸 수도 있다. **`--port`는 UDP 포트, `--web-port`는 웹 포트**다.

```sh
uv run f1re serve                          # UDP 20777 + 웹 8093
uv run f1re serve --port 20777 --web-port 8093
uv run f1re serve --web-host 127.0.0.1     # 서버 안에서만 웹을 받는다(서비스 기본)
```

### 레이스 엔지니어 ON/OFF

웹 서버(TCP 8093)는 항상 뜨고, 게임 중에만 필요한 엔진(UDP 수신·분석·무전·
TTS·DB 기록)은 켜고 끌 수 있다. **처음 실행하면 OFF**이고, 마지막 상태는
`data/engine-state.json`에 남아 재시작 뒤에도 유지된다.

- 대시보드 우측 아래 **톱니바퀴** 버튼 → **레이스 엔지니어** 토글
- `uv run f1re serve --engine on` / `--engine off` — 시작 상태 덮어쓰기
- `GET /api/engine` → `{"state": "on"|"off"}`,
  `POST /api/engine {"state": "on"|"off"}` → 전환 후 상태
- `GET /health` → `{"status": "ok", "engine": "on"|"off"}` (모니터링용, 항상 200)

OFF일 때는 UDP 20777 소켓이 닫히고(`ss -lunp | grep 20777`에 안 보임) 백그라운드
루프·TTS 호출·DB 쓰기가 모두 멈춘다. 화면·정적 파일·health·`/api/engine`만
동작하고 나머지 API는 503 `{"engine": "off"}`로 답한다. 방화벽 규칙은 그대로
두어도 된다(프로세스가 포트를 열지 않는다).

방화벽에서는 게임 텔레메트리 UDP만 연다(예: Ubuntu `ufw`. 이미 허용되어 있으면
건너뛴다). 웹 8093은 `127.0.0.1`에서만 받으므로 열지 않는다.

```sh
sudo ufw allow 20777/udp
sudo ufw status
```

`firewalld`를 쓰면 같은 뜻으로:

```sh
sudo firewall-cmd --permanent --add-port=20777/udp
sudo firewall-cmd --reload
```

### nginx

같은 서버의 nginx가 HTTPS를 받아 `127.0.0.1:8093`으로 넘긴다. 대시보드
(`/ws/state`)와 무전(`/ws/radio`)이 WebSocket을 쓰므로 업그레이드 헤더를 꼭
넘긴다. 빠지면 페이지는 열려도 값이 갱신되지 않고 무전이 연결되지 않는다.

```nginx
server {
    listen 443 ssl http2;
    server_name <도메인>;
    # ssl_certificate ... (서버의 인증서 설정)

    location / {
        proxy_pass http://127.0.0.1:8093;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;   # 무전 WebSocket이 오래 열려 있다
    }
}
```

HTTPS로 열면 페이지가 스스로 `wss://`로 연결한다.

## 4. Qwen 키(시크릿)

무전 음성 합성에는 Qwen API 키가 필요하다. 키 값을 레포·서비스 파일·문서·로그에
쓰지 않는다.

- 코드(`src/f1re/secrets.py`)는 `~/.claude/skills/qwen-key/get-qwen-key.py`를 실행해
  공용 시크릿 모듈의 **`qwen` 항목**을 읽는다. 설정 키 이름은 `[tts] secret_key = "qwen"`이다.
- 따라서 서비스가 실행되는 사용자(기본 `golony`)의 홈에 위 도우미와
  `~/.claude/bin/claude_secrets.py`가 있어야 한다. 키를 환경변수나 서비스 파일에
  넣지 않는다.
- 값이 출력되지 않는 확인:

  ```sh
  uv run python -c "from f1re.secrets import load_qwen_key; load_qwen_key(); print('Qwen 키 확인 OK')"
  ```

  실패하면 `SecretsError` 메시지만 나온다(키 값은 메시지에 없다). 이때는
  `~/.claude` 설치 상태와 `qwen` 항목 등록을 확인한다.

## 5. 서비스 등록·시작·로그

`deploy/f1re.service`는 systemd **사용자 서비스**다(`sudo` 불필요).

```sh
cd /home/golony/dev/repo/f1-race-engineer
command -v uv        # 다르면 아래 파일의 ExecStart를 그 경로로 고친다
nano deploy/f1re.service   # 필요할 때만: uv 경로, 체크아웃 위치, 포트 인자

mkdir -p ~/.config/systemd/user
cp deploy/f1re.service ~/.config/systemd/user/f1re.service
systemctl --user daemon-reload
systemctl --user enable --now f1re
loginctl enable-linger "$USER"   # 로그아웃·재부팅 뒤에도 서비스 유지
```

상태·로그:

```sh
systemctl --user status f1re
journalctl --user -u f1re -f
```

정상이면 로그에 아래 줄이 보인다. 엔진은 마지막 저장 상태로 시작한다.

```
웹 대시보드: http://localhost:8093
레이스 엔지니어: 꺼짐 (대시보드 우측 아래 설정에서 켜면 UDP 수신을 시작합니다)
```

대시보드에서 켜면 서비스 로그에 아래 줄이 추가된다.

```
UDP 수신 대기: 0.0.0.0:20777 (Ctrl+C로 중단)
```

서비스 상태 확인(엔진 상태 포함):

```sh
curl -s http://localhost:8093/health
# {"status":"ok","engine":"off"}  또는  {"status":"ok","engine":"on"}
```

중지·재시작·해제:

```sh
systemctl --user restart f1re
systemctl --user stop f1re
systemctl --user disable --now f1re
```

업데이트:

```sh
cd /home/golony/dev/repo/f1-race-engineer
git pull
uv sync
systemctl --user restart f1re
```

## 6. 게임(F1 25) 텔레메트리 설정

게임 PC에서 **설정 → 텔레메트리 설정(Telemetry Settings)** 을 연다.

| 항목 | 값 |
|---|---|
| UDP 텔레메트리 | **켜기(On)** |
| UDP 브로드캐스트 | **끄기(Off)** — 서버 IP로 직접 보낸다 |
| IP 주소(UDP IP Address) | **홈서버의 LAN IP** (예: `192.168.0.10`) |
| 포트(UDP Port) | **20777** |
| UDP 전송률(UDP Send Rate) | **60Hz** 권장 |
| UDP 형식(UDP Format) | **2025** |

서버 IP는 서버에서 `hostname -I`(또는 `ip addr`)로 확인한다. 게임 PC와 서버가
같은 공유기(같은 서브넷)에 있어야 한다. 브로드캐스트를 켜면 패킷이 다른 기기로도
가므로 끈다.

## 7. 접속 확인

1. **태블릿**: 브라우저에서 `https://<도메인>/` — 대시보드가 뜬다. 처음에는
   "레이스 엔지니어가 꺼져 있습니다" 안내가 보이므로, 우측 아래 **톱니바퀴** 버튼을
   눌러 **레이스 엔지니어**를 켠다. 게임을 시작하면 차량 상태·서킷 지도가 갱신된다.
2. **게임 PC(무전)**: 브라우저에서 `https://<도메인>/radio` 를 열고
   **무전 연결** 버튼을 한 번 누른다(브라우저 자동 재생 정책 때문). 엔진이 꺼져
   있으면 페이지에 꺼짐 안내가 보이고, 대시보드에서 켜면 연결 버튼이 되살아난다.
   연결 상태와 마지막 무전 자막이 크게 보이고, 연결이 끊기면 페이지가 스스로
   다시 연결한다.
   게임 화면과 함께 보려면 브라우저 창을 옆 모니터·태블릿에 두거나
   `Alt+Tab`으로 전환한다. 탭을 백그라운드에 두어도 소리는 유지된다.

## 8. 게임 없이 확인(선택)

레포가 있는 다른 PC(또는 서버 자신)에서 데모 패킷을 서버로 보내 수신·웹·무전을
시험할 수 있다.

```sh
# 다른 PC에서 서버로
uv run f1re-demo --track suzuka --mode tt --host <서버 IP> --port 20777 --laps 1 --speed 5

# 서버 자신에서
uv run f1re-demo --track suzuka --mode tt --host 127.0.0.1 --port 20777 --laps 1 --speed 5
```

보내는 동안 태블릿 대시보드가 움직이는지, `/radio`에서 무전이 들리는지 확인한다.
(무전은 타이어·브레이크·연료·손상·페널티 같은 위험 기준을 넘을 때 나므로,
`--scenario src/f1re/demo_sender/scenarios/alerts.yaml`를 함께 주면 더 빨리 확인할 수 있다.)

## 9. 문제 해결

| 증상 | 확인 |
|---|---|
| 대시보드가 안 열린다 | `systemctl --user status f1re`, `ss -ltnp \| grep 8093`(`127.0.0.1:8093`이어야 한다), `curl http://127.0.0.1:8093/`, nginx 설정·`allow` 범위 |
| 화면은 뜨는데 값·무전이 안 온다 | nginx에 WebSocket 업그레이드 헤더(`Upgrade`·`Connection`)가 있는지 |
| 게임을 켜도 화면이 그대로다 | 대시보드 우측 아래 설정에서 레이스 엔지니어가 켜져 있는지(`curl -s http://127.0.0.1:8093/health`), 게임 텔레메트리 IP·포트(20777)·형식 2025, `ss -lunp \| grep 20777`, 방화벽의 20777/udp |
| OFF인데 20777이 안 열린다 | 정상이다. 엔진을 켜야 UDP 소켓이 열린다(설계상 OFF는 모든 수신을 멈춘다). |
| 무전 소리가 안 난다 | `/radio`에서 **무전 연결**을 눌렀는지, 서버 로그의 Qwen 오류, 4장의 키 확인 |
| 시작 효과음이 클릭음이다 | `uv run python tools/fetch_assets.py`로 `assets/radio-intro.wav` 받기 |
| 재부팅 뒤 서비스가 없다 | `loginctl enable-linger "$USER"` 실행 여부, `systemctl --user is-enabled f1re` |
| 웹 포트가 이미 쓰인다 | `ss -ltnp \| grep 8093`으로 사용 프로세스 확인, `[web] port` 또는 `--web-port` 변경(nginx `proxy_pass`도 함께) |

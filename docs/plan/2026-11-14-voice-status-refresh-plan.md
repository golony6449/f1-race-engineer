# 음성 오프라인 이유 표시와 상태 새로고침 계획 (SCRUM-92)

일감: https://golony.atlassian.net/browse/SCRUM-92

## 배경

2026-11-14 14:16 Qwen TTS가 한 번 실패한 뒤 무전 페이지가 계속 "음성 오프라인"으로 남았다.
API는 정상이었다(첫 조각 0.55초 안팎). `VoiceCache`는 합성이 한 번 성공해야만 온라인으로
돌아가고(`_set_online`), 오프라인으로 바뀔 때 로그를 남기지 않으며, 실패 이유(`last_error`)를
화면에 보여 주지 않아 원인을 알 수 없었다.

## Global Constraints

- 주석·문서·커밋 메시지는 한국어. 테스트는 `uv run pytest`.
- 비밀값(API 키)은 로그·상태·화면에 넣지 않는다. `last_error`는 `TTSUnavailable` 메시지로
  이미 비밀값이 없다(`src/f1re/tts/qwen.py`). 그대로 쓰되 새 메시지를 만들 때도 키를 넣지 않는다.
- 자동 확인은 비용이 드는 Qwen 호출이다. **오프라인이면서 텔레메트리를 받고 있을 때만** 한다.
  게임을 안 할 때(최근 패킷 없음)나 온라인일 때는 절대 부르지 않는다.
- 의존성 파일(`pyproject.toml`, `uv.lock`)은 바꾸지 않는다.

### 작업 1: 오프라인 전환 로그와 상태의 실패 이유

**Files:**
- Modify: `src/f1re/tts/cache.py`
- Modify: `src/f1re/web/radio_api.py`
- Test: `tests/tts/test_cache.py`
- Test: `tests/web/test_radio_ws.py`

1. `VoiceCache._set_offline(error)`: 지금 상태가 ONLINE일 때만(전환 순간에만)
   `logger.warning("Qwen TTS 실패로 음성 오프라인: %s", error)`를 남긴다. 이미 오프라인이면
   `_last_error`만 바꾸고 로그는 남기지 않는다(실패마다 찍지 않는다).
2. `RadioService.status()`의 `"voice"`에 `"reason"` 키를 넣는다.
   - 오프라인이고 캐시에 `last_error`가 있으면 그 문자열, 없으면 `None`.
   - 온라인이면 `None`.
   - 캐시가 없을 때(`self._cache is None`)도 `None`.
   - `STATUS_VOICE_*` 상수 dict는 바꾸지 말고, `dict(...)`로 복사한 뒤 `reason`을 넣는다.
   - 기존 `state`·`label` 값은 그대로 둔다(기존 테스트가 `==`로 비교하는 곳은 `reason: None`을 넣어 고친다).
3. 테스트:
   - 첫 실패에서 warning이 1번 나오고, 두 번째 실패에서는 안 나온다(`caplog`).
   - 실패 뒤 `status()["voice"]["reason"]`이 실패 메시지와 같다. 성공 뒤에는 `None`.

### 작업 2: 음성 다시 확인 API와 자동 확인

**Files:**
- Modify: `src/f1re/tts/cache.py`
- Modify: `src/f1re/web/radio_api.py`
- Test: `tests/tts/test_cache.py`
- Test: `tests/web/test_radio_ws.py`

1. `VoiceCache.check(self) -> VoiceStatus` (async): 짧은 문장 `PROBE_TEXT = "라디오 체크."`를
   `self._tts.synthesize()`로 한 번 합성한다(캐시 파일은 만들지 않는다). 성공하면 `_set_online()`,
   `TTSUnavailable`이면 `_set_offline(exc)`. 지금 상태를 돌려준다. 그 밖의 예외는 그대로 올린다.
2. `RadioService.check_voice(self) -> dict` (async): 캐시가 없으면 아무것도 부르지 않고 `status()`를
   돌려준다. 있으면 `await cache.check()` 뒤 `_broadcast_status()`를 부르고 `status()`를 돌려준다.
   동시에 여러 번 눌려도 Qwen 호출이 겹치지 않게 `asyncio.Lock`으로 감싼다(잠겨 있으면 기다렸다가
   이미 끝난 결과를 돌려줘도 된다).
3. `POST /api/voice/check` 라우트(radio_api의 `router`): 엔진의 `RadioService`(`ws_radio`가 서비스를
   찾는 방식과 같게 `app.state.engine.radio` 또는 `app.state.radio`)를 찾아 `check_voice()` 결과를
   `{"status": ...}`로 돌려준다. 서비스가 없거나 엔진이 꺼져 있으면 Qwen을 부르지 않고
   `{"status": <오프라인·미연결 기본 상태>}`를 200으로 돌려준다.
4. 자동 확인 루프 `RadioService._voice_check_loop()`를 `ensure_started()`의 작업 목록에 추가한다.
   - `VOICE_CHECK_S = 30.0`마다 깬다(생성자 인자 `voice_check_s`로 바꿀 수 있게, 테스트용).
   - 조건을 모두 만족할 때만 `check_voice()`를 부른다: 캐시가 있음, 캐시가 오프라인,
     텔레메트리 수신 중.
   - 텔레메트리 수신 중: `self._store.snapshot()`의 진단 정보에서 마지막 수신 시각
     (`StateStore.diagnostics.last_recv_ns`, `time.time_ns()` 기준)을 읽어
     `self._clock() - last_recv_ns <= TELEMETRY_FRESH_S(5초)`이면 수신 중이다. 값이 없으면 수신 중이 아니다.
     snapshot에서는 `snapshot["diagnostics"]["last_recv_ns"]`로 꺼낸다(`Diagnostics.to_dict()`).
   - 루프 안 예외는 `logger.exception`으로 남기고 계속 돈다(`_alert_loop`와 같은 방식).
5. 테스트(가짜 캐시·가짜 store·가짜 clock으로, 실제 네트워크 없이):
   - 오프라인 + 최근 패킷 있음 → 루프가 `check()`를 부른다.
   - 오프라인 + 패킷 없음(또는 5초보다 오래됨) → 부르지 않는다.
   - 온라인 + 최근 패킷 → 부르지 않는다.
   - `POST /api/voice/check`가 `check()`를 한 번 부르고, 성공하면 응답 `status.voice.state`가 `online`.
   - 서비스가 없을 때 `POST /api/voice/check`가 200과 오프라인 상태를 돌려준다.

### 작업 3: 화면에 실패 이유와 새로고침 버튼

**Files:**
- Modify: `src/f1re/web/static/radio.html`
- Modify: `src/f1re/web/static/radio.js`
- Modify: `src/f1re/web/static/carstate.js`
- Test: `tests/web/test_api.py`

1. 무전 페이지(`radio.html`/`radio.js`):
   - 음성 상태 옆(`#voice` 근처)에 실패 이유를 보여 준다. `status.voice.reason`이 있으면
     `음성 오프라인 (<reason>)`처럼 붙이고, 없으면 지금처럼 label만. `textContent`로만 넣는다(HTML 삽입 금지).
   - "상태 새로고침" 버튼(`id="voice-check"`)을 둔다. 누르면 버튼을 잠시 비활성화하고
     `fetch("/api/voice/check", {method: "POST"})` 결과의 `status`를 `handleStatus({status})`로 반영한 뒤
     다시 활성화한다. 실패해도 버튼은 다시 켠다.
2. 대시보드(`carstate.js`의 `renderRadioStatus`): 오프라인이고 `voice.reason`이 있으면 노드의
   `title`에 이유를 넣는다(문구는 지금 그대로). 온라인이면 `title`을 비운다.
3. 테스트: `GET /radio` HTML에 `id="voice-check"` 버튼이 있다(`tests/web/test_api.py`나
   무전 페이지 HTML을 보는 기존 테스트 파일에 추가).

## Review Focus

1. 자동 확인이 오프라인 + 텔레메트리 수신 중(최근 5초 안 패킷)일 때만 Qwen을 부르는가. 온라인이거나 게임을 안 할 때 호출이 새지 않는가.
2. 오프라인 warning이 전환 순간에만 한 번 나오는가.
3. 상태·로그·화면 어디에도 API 키가 들어가지 않는가. 화면은 `textContent`로만 넣는가.
4. `POST /api/voice/check`가 엔진 OFF·서비스 없음에서 Qwen을 부르지 않고 200을 주는가.
5. 기존 `status()` 응답 모양(state·label·radio_page)이 깨지지 않았는가.

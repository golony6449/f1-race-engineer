# Qwen TTS 확인 노트

- 확인 날짜: 2026-11-12
- 확인한 문서: Alibaba Cloud Model Studio 국제(싱가포르) 리전 공식 문서
- 구현: `src/f1re/tts/qwen.py` (일반 합성·SSE 스트리밍), `src/f1re/tts/radiofx.py` (무전 효과)
- 샘플 도구: `tools/voice_samples.py`

## 1. 공식 문서에서 확인한 API

### 일반(비실시간) 합성

- 엔드포인트: `POST https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`
- 헤더: `Authorization: Bearer <키>`, `Content-Type: application/json`
- 본문:

  ```json
  {
    "model": "qwen3-tts-flash",
    "input": {
      "text": "3번, 브레이크 늦음. 5미터.",
      "voice": "Sohee",
      "language_type": "Korean"
    }
  }
  ```

- `input.text` 최대 600자. `input.language_type`은 선택이며 기본 `Auto`, `Korean`을 지원한다.
- 응답: `output.audio.url`(WAV, 24시간 유효), `output.audio.expires_at`, `usage.characters`.
  비실시간 응답의 `output.audio.data`는 비어 있다.
  - **PL 실측(2026-11-12): 실제 `output.audio.url`은 `http://...` Alibaba OSS 서명
    주소로 온다.** 클라이언트는 같은 주소의 `https://`를 먼저 시도하고, 실패하면
    원래 `http://`로 받는다. 두 경우 모두 `Authorization` 헤더는 보내지 않는다.
- 오류 응답: `status_code`, `request_id`, `code`, `message` (HTTP 400/401/404/500 등).

### SSE 스트리밍

- 같은 엔드포인트에 헤더 `X-DashScope-SSE: enable`을 준다.
- 응답은 `data: {JSON}` 이벤트의 연속이다. 중간 이벤트의 `output.audio.data`는
  base64 PCM(24kHz, 16bit, mono)이고, 마지막 이벤트는 `data`가 비고
  `output.audio.url`이 온다.
- 공식 문서에는 별도 이벤트 이름이나 `[DONE]` 표시가 없다. 구현은 빈 줄 단위로
  이벤트를 끊고, `[DONE]`이 와도 무시한다.
- 첫 조각까지 2초(설정값) 안에 오지 않으면 `TTSUnavailable`로 올린다. 첫 조각
  이후에는 전체 스트림 마감을 풀고 각 읽기에 httpx read 타임아웃을 적용한다.

### 실시간(WebSocket) 모델 — 이번 구현 범위 밖

- 모델: `qwen3-tts-flash-realtime`
- 엔드포인트(국제 리전): `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime`
- 접속 헤더: `Authorization: Bearer <키>`
- 흐름: 접속 → `session.update`(voice, mode=`server_commit`|`commit`,
  `language_type`, `response_format`, `sample_rate`=24000) → `input_text_buffer.append`
  → (`commit` 모드면 `input_text_buffer.commit`) → `response.audio.delta`(base64)
  → `response.audio.done` → `session.finish`.
- 단가(2026-11-12 확인): 입력 글자 수 기준, 출력 무료.
  - 싱가포르(국제): **CNY 0.954101 / 1만 자**
  - 베이징: CNY 1 / 1만 자
  - 분당 요청 한도 180 RPM
- 이번 작업은 계획대로 SSE 스트리밍(`stream`)만 구현했다. 실시간 모델이 필요하면
  작업 11에서 위 WebSocket 흐름을 추가한다.

## 2. 한국어 지원 모델·보이스

- 한국어 지원 모델: `qwen3-tts-flash`, `qwen3-tts-flash-realtime`
- 보이스 목록: 두 모델 모두 48개 보이스를 제공하며, 문서상 **48개 모두 한국어를
  지원**한다.
- 한국어 전용으로 소개된 보이스: **Sohee** (따뜻하고 밝은 한국어 여성 보이스)
- 그 외 대표 후보: Cherry, Serena, Ethan, Chelsie (모두 한국어 지원)
- `config/default.toml`의 임시 기본값은 `Sohee`. SCRUM-50에서 사용자가 샘플을
  듣고 고른 뒤 바꾼다.
- `tools/voice_samples.py`의 기본 후보 목록: Sohee, Cherry, Serena, Ethan, Chelsie.
  `--voices`로 바꿀 수 있다.

## 3. 한글 글자 수 계산

- 과금은 입력 텍스트의 문자 수 기준이며, 응답의 `usage.characters`로 확인할 수 있다.
- 파이썬 `len(text)`는 유니코드 코드 포인트 수를 세므로 한글 음절 1자 = 1로 센다.
  샘플 도구의 "글자" 열은 `len(text)` 값이다.
- 공식 문서는 공백·문장부호를 문자 수에 포함하는지 명시하지 않는다. 실제 호출의
  `usage.characters`와 `len(text)`를 대조해 확인한다(PL 실측 예정).
- 요청 한 건의 텍스트는 최대 600자다.

## 4. 측정 지연

| 항목 | 측정 | 비고 |
| --- | --- | --- |
| 일반 합성: 요청 → WAV 수신 | 789~2109ms (중앙값 약 1100ms) | PL 실측 2026-11-12, 한국 가정망, 5보이스×5문장 |
| SSE 스트리밍: 요청 → 첫 PCM 조각 | 377~560ms (중앙값 약 430ms) | 목표 1초 이내 충족 |
| SSE 스트리밍: 전체 수신 | 654~1219ms | 7~18자 문장 |
| 실시간 WebSocket: 첫 조각 | 미측정 | 이번 구현 범위 밖 |

- 결론: 캐시 미스 문장은 SSE 스트리밍으로 첫 소리 약 0.4~0.6초. 일반 합성(약 1초+)은 미리 만들기(prefetch)에만 쓴다.

- 목표: 코너 탈출 → 첫 소리 1초 이내(설계 6.8, 7장).
- 샘플 도구는 실제 API를 호출해 측정하고 `samples/latency.md`에 표를 저장한다.
  이 파일과 wav는 `.gitignore` 대상이라 커밋하지 않는다.

## 5. 계획·가정과 다르게 확인된 것

- PL이 전달한 가정(엔드포인트, `Authorization`, 본문 `input`에 text·voice·
  `language_type`, `output.audio.url`, SSE `output.audio.data`)은 공식 문서와
  일치한다.
- 계획에는 실시간 모델을 스트리밍에 쓴다고 되어 있지만, 이번 작업은 PL 지시대로
  `qwen3-tts-flash`의 SSE 스트리밍을 구현했다. 실시간 WebSocket 모델은 단가·방식만
  정리했다.
- 공식 문서 기준 입력 600자 제한이 있어 클라이언트에서 먼저 검사한다
  (`TTSInvalidRequest`). 무전 문장은 짧아 실제로 닿지 않는다.
- API 오류 메시지 원문은 예외에 넣지 않고 `code`·`request_id`만 남긴다.
- 오디오 다운로드 URL은 서명된 다른 호스트이므로 `Authorization` 헤더를 붙이지
  않는다.
- PL 실측(2026-11-12): 5개 보이스 × 5문장 25건이 모두 `http://` URL 때문에
  실패했다(기존 클라이언트가 https만 허용). http/https를 모두 받고 https를 먼저
  시도한 뒤 http로 폴백하도록 고쳤다. 고친 뒤 PL 재실행에서 25건 모두 정상.

## 출처

- Qwen-TTS HTTP API: https://www.alibabacloud.com/help/en/model-studio/qwen-tts-api
- Qwen-TTS 보이스 목록: https://www.alibabacloud.com/help/en/model-studio/qwen-tts-voice-list
- Qwen3-TTS-Flash-Realtime 모델: https://help.aliyun.com/en/model-studio/qwen3-tts-flash-realtime
- 실시간 WebSocket 접속 가이드: https://www.alibabacloud.com/help/en/model-studio/interactive-process-of-qwen-tts-realtime-synthesis
- 실시간 클라이언트 이벤트: https://www.alibabacloud.com/help/en/model-studio/qwen-tts-realtime-client-events
- 모델 단가: https://www.alibabacloud.com/help/en/model-studio/model-pricing

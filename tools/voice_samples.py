"""Qwen TTS 한국어 보이스 샘플과 첫 소리 지연을 만드는 도구.

실행:
    uv run python tools/voice_samples.py --out samples

보이스마다 계획의 무전 문장 5개를 합성해 `samples/<voice>/<n>-raw.wav`와
무전 효과를 입힌 `<n>-radio.wav`를 저장하고, 합성·스트리밍 지연을 표로
출력해 `samples/latency.md`에도 쓴다. 실제 API 키가 필요하므로 보이스
선택(SCRUM-50) 전에 PL이 한 번 실행한다.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from f1re.config import ConfigError, load_config
from f1re.tts.qwen import QwenTTS, TTSError
from f1re.tts.radiofx import RadioFxConfig, apply_radio_fx_wav

SENTENCES: tuple[str, ...] = (
    "3번, 브레이크 늦음. 5미터.",
    "7번 탈출, 스트로틀 늦음.",
    "앞차 0.6. 다음 직선 DRS.",
    "박스, 박스.",
    "3번 좋아짐. 다음 7번.",
)

# 문서상 모든 보이스가 한국어를 지원한다. 사용자가 고른 Katerina(2026-11-13)를
# 맨 앞에 두고, 비교용 대표 보이스를 뒤에 둔다.
KOREAN_VOICES: tuple[str, ...] = (
    "Katerina",
    "Sohee",
    "Cherry",
    "Serena",
    "Ethan",
    "Chelsie",
)


class TTSClient(Protocol):
    async def synthesize(self, text: str, voice: str | None = None) -> bytes: ...

    def stream(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]: ...


@dataclass
class SampleResult:
    voice: str
    index: int
    text: str
    chars: int
    synth_ms: float | None = None
    stream_first_ms: float | None = None
    stream_total_ms: float | None = None
    raw_file: str | None = None
    radio_file: str | None = None
    synth_error: str | None = None
    stream_error: str | None = None

    @property
    def status(self) -> str:
        """합성과 스트림 결과를 따로 적는다. 둘은 서로 독립으로 측정한다."""
        if self.synth_error is None and self.stream_error is None:
            return "정상"
        synth = f"합성 오류: {self.synth_error}" if self.synth_error else "합성 정상"
        stream = (
            f"스트림 오류: {self.stream_error}" if self.stream_error else "스트림 정상"
        )
        return f"{synth} / {stream}"


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip())
    return cleaned.strip("-") or "voice"


async def _measure_stream(
    tts: TTSClient, text: str, voice: str
) -> tuple[float, float]:
    started = time.perf_counter()
    first_ms: float | None = None
    async for _chunk in tts.stream(text, voice):
        if first_ms is None:
            first_ms = (time.perf_counter() - started) * 1000.0
    total_ms = (time.perf_counter() - started) * 1000.0
    if first_ms is None:
        raise TTSError("스트림에서 오디오를 받지 못했습니다")
    return first_ms, total_ms


async def generate_samples(
    tts: TTSClient,
    out_dir: str | Path,
    *,
    voices: Sequence[str] = KOREAN_VOICES,
    sentences: Sequence[str] = SENTENCES,
) -> list[SampleResult]:
    """보이스 × 문장 샘플을 만들고 측정 결과를 돌려준다."""
    output = Path(out_dir)
    results: list[SampleResult] = []
    for voice in voices:
        voice_dir = output / _slug(voice)
        voice_dir.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(sentences, start=1):
            raw_path = voice_dir / f"{index}-raw.wav"
            radio_path = voice_dir / f"{index}-radio.wav"
            result = SampleResult(
                voice=voice,
                index=index,
                text=text,
                chars=len(text),
            )
            # 합성과 스트림은 서로 독립으로 시도하고 오류도 따로 기록한다.
            try:
                started = time.perf_counter()
                wav = await tts.synthesize(text, voice)
                result.synth_ms = (time.perf_counter() - started) * 1000.0
                raw_path.write_bytes(wav)
                radio_path.write_bytes(apply_radio_fx_wav(wav))
                result.raw_file = str(raw_path.relative_to(output))
                result.radio_file = str(radio_path.relative_to(output))
            except (TTSError, ValueError, OSError) as exc:
                result.synth_error = _describe(exc)

            try:
                first_ms, total_ms = await _measure_stream(tts, text, voice)
                result.stream_first_ms = first_ms
                result.stream_total_ms = total_ms
            except (TTSError, ValueError, OSError) as exc:
                result.stream_error = _describe(exc)
            results.append(result)
    return results


def _describe(exc: BaseException) -> str:
    return str(exc).strip() or type(exc).__name__


def _format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def render_table(results: Sequence[SampleResult]) -> str:
    """결과를 Markdown 표로 만든다."""
    lines = [
        "| 보이스 | # | 글자 | 합성 ms | 스트림 첫 조각 ms | 스트림 전체 ms | 상태 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        status = result.status
        lines.append(
            "| "
            + " | ".join(
                [
                    result.voice,
                    str(result.index),
                    str(result.chars),
                    _format_ms(result.synth_ms),
                    _format_ms(result.stream_first_ms),
                    _format_ms(result.stream_total_ms),
                    status.replace("|", "/"),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def write_latency_report(
    results: Sequence[SampleResult],
    out_dir: str | Path,
    *,
    model: str,
    generated_at: datetime | None = None,
) -> Path:
    """지연 표를 `latency.md`로 저장한다."""
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at or datetime.now(timezone.utc)
    fx = RadioFxConfig()
    header = [
        "# Qwen TTS 보이스 샘플 지연",
        "",
        f"- 모델: `{model}`",
        f"- 측정 시각(UTC): {timestamp.isoformat(timespec='seconds')}",
        (
            "- 무전 효과: "
            f"{fx.highpass_hz:.0f}~{fx.lowpass_hz:.0f}Hz, 잡음 {fx.noise_level}, "
            f"클릭 {fx.click_ms:.0f}ms (version {fx.version})"
        ),
        "- 지연은 실행한 PC에서 실제 API를 호출해 잰 값이며 네트워크 시간을 포함한다.",
        "- 스트림 첫 조각 지연은 SSE로 받은 첫 PCM 조각까지의 시간이다.",
        "",
        render_table(results),
        "",
    ]
    path = output / "latency.md"
    path.write_text("\n".join(header), encoding="utf-8")
    return path


def _parse_voices(raw: str) -> tuple[str, ...]:
    voices = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not voices:
        raise argparse.ArgumentTypeError("보이스를 하나 이상 주세요")
    return voices


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qwen 한국어 보이스 샘플과 첫 소리 지연을 측정한다."
    )
    parser.add_argument("--out", default="samples", help="샘플 출력 폴더 (기본 samples)")
    parser.add_argument(
        "--voices",
        type=_parse_voices,
        default=KOREAN_VOICES,
        help="쉼표로 구분한 보이스 목록 (기본: " + ",".join(KOREAN_VOICES) + ")",
    )
    args = parser.parse_args(argv)

    try:
        app_config = load_config()
    except ConfigError as exc:
        print(f"설정을 읽지 못했습니다: {exc}")
        return 2

    async def run() -> list[SampleResult]:
        async with QwenTTS(app_config.tts) as tts:
            return await generate_samples(tts, args.out, voices=args.voices)

    try:
        results = asyncio.run(run())
    except TTSError as exc:
        print(f"음성 샘플을 만들지 못했습니다: {exc}")
        return 1

    print(render_table(results))
    report = write_latency_report(results, args.out, model=app_config.tts.model)
    print(f"\n저장: {Path(args.out)} (지연 표: {report})")
    synth_errors = sum(1 for result in results if result.synth_error)
    stream_errors = sum(1 for result in results if result.stream_error)
    if synth_errors or stream_errors:
        print(
            f"합성 오류 {synth_errors}건, 스트림 오류 {stream_errors}건. "
            "위 표의 상태를 확인하세요."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

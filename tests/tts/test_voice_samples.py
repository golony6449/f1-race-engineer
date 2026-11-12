"""보이스 샘플 도구: 네트워크 없이 가짜 TTS로 파일·표를 만든다."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from pathlib import Path

from f1re.tts.radiofx import pcm_to_wav
from f1re.tts.qwen import TTSUnavailable
from tools.voice_samples import (
    KOREAN_VOICES,
    SENTENCES,
    generate_samples,
    render_table,
    write_latency_report,
)

FAKE_WAV = pcm_to_wav(b"\x00\x00" * 2400, 24_000)


class FakeTTS:
    def __init__(
        self, fail_synth_on: str | None = None, fail_stream_on: str | None = None
    ) -> None:
        self.synth_calls: list[tuple[str, str | None]] = []
        self.stream_calls: list[tuple[str, str | None]] = []
        self.fail_synth_on = fail_synth_on
        self.fail_stream_on = fail_stream_on

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        self.synth_calls.append((text, voice))
        if self.fail_synth_on and self.fail_synth_on in text:
            raise TTSUnavailable("가짜 실패")
        return FAKE_WAV

    async def stream(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        self.stream_calls.append((text, voice))
        if self.fail_stream_on and self.fail_stream_on in text:
            raise TTSUnavailable("가짜 스트림 실패")
        yield b"\x00\x00" * 120


def test_sentences_follow_plan() -> None:
    assert SENTENCES == (
        "3번, 브레이크 늦음. 5미터.",
        "7번 탈출, 스트로틀 늦음.",
        "앞차 0.6. 다음 직선 DRS.",
        "박스, 박스.",
        "3번 좋아짐. 다음 7번.",
    )
    assert KOREAN_VOICES[0] == "Katerina"
    assert "Sohee" in KOREAN_VOICES


async def test_generate_samples_writes_raw_radio_and_report(tmp_path: Path) -> None:
    tts = FakeTTS()

    results = await generate_samples(tts, tmp_path, voices=("Sohee",))

    assert len(results) == len(SENTENCES)
    assert all(result.synth_error is None for result in results)
    assert all(result.stream_error is None for result in results)
    assert all(result.status == "정상" for result in results)
    assert all(result.synth_ms is not None for result in results)
    assert all(result.stream_first_ms is not None for result in results)
    assert all(result.chars == len(result.text) for result in results)
    for index in range(1, len(SENTENCES) + 1):
        assert (tmp_path / "Sohee" / f"{index}-raw.wav").is_file()
        assert (tmp_path / "Sohee" / f"{index}-radio.wav").is_file()

    report = write_latency_report(
        results,
        tmp_path,
        model="qwen3-tts-flash",
        generated_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
    )
    text = report.read_text(encoding="utf-8")
    assert "| 보이스 |" in text
    assert "Sohee" in text
    assert "qwen3-tts-flash" in text
    assert "2026-11-12" in text


async def test_synth_failure_still_measures_stream(tmp_path: Path) -> None:
    tts = FakeTTS(fail_synth_on="박스")

    results = await generate_samples(tts, tmp_path, voices=("Sohee",))

    failed = [result for result in results if result.synth_error]
    assert len(failed) == 1
    result = failed[0]
    assert result.synth_error == "가짜 실패"
    assert result.stream_error is None
    # 합성이 실패해도 스트림 측정은 따로 시도한다.
    assert result.stream_first_ms is not None
    assert ("박스, 박스.", "Sohee") in tts.stream_calls
    assert result.status == "합성 오류: 가짜 실패 / 스트림 정상"
    table = render_table(results)
    assert "합성 오류: 가짜 실패 / 스트림 정상" in table
    # 실패한 문장만 빠지고 나머지 파일은 남는다.
    assert (tmp_path / "Sohee" / "1-raw.wav").is_file()
    assert not (tmp_path / "Sohee" / "4-raw.wav").exists()


async def test_stream_failure_keeps_synth_result(tmp_path: Path) -> None:
    tts = FakeTTS(fail_stream_on="박스")

    results = await generate_samples(tts, tmp_path, voices=("Sohee",))

    failed = [result for result in results if result.stream_error]
    assert len(failed) == 1
    result = failed[0]
    assert result.synth_error is None
    assert result.stream_error == "가짜 스트림 실패"
    assert result.synth_ms is not None
    assert result.stream_first_ms is None
    assert result.status == "합성 정상 / 스트림 오류: 가짜 스트림 실패"
    # 합성 파일은 정상으로 남는다.
    assert (tmp_path / "Sohee" / "4-raw.wav").is_file()
    assert (tmp_path / "Sohee" / "4-radio.wav").is_file()


async def test_generate_samples_uses_requested_voice(tmp_path: Path) -> None:
    tts = FakeTTS()

    await generate_samples(
        tts,
        tmp_path,
        voices=("Cherry",),
        sentences=("박스, 박스.",),
    )

    assert tts.synth_calls == [("박스, 박스.", "Cherry")]
    assert tts.stream_calls == [("박스, 박스.", "Cherry")]
    assert (tmp_path / "Cherry" / "1-radio.wav").is_file()


def test_render_table_formats_missing_values() -> None:
    from tools.voice_samples import SampleResult

    results: Sequence[SampleResult] = [
        SampleResult(
            voice="Sohee",
            index=1,
            text="박스, 박스.",
            chars=6,
            raw_file="Sohee/1-raw.wav",
            radio_file="Sohee/1-radio.wav",
        )
    ]

    table = render_table(results)

    assert "| Sohee | 1 | 6 | - | - | - | 정상 |" in table

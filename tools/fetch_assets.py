"""무전 시작 효과음을 받아 assets/에 둔다.

저작권 때문에 효과음 파일은 레포에 넣지 않고 받아 온다. 실행:

    uv run python tools/fetch_assets.py [--force]

`config/default.toml`의 `[tts]`에 적힌 주소에서 httpx로 받아, miniaudio로
24kHz 16bit mono로 디코딩하고 앞뒤 무음(|x| < 0.01)을 잘라 WAV로 저장한다.
이미 파일이 있으면 건너뛴다(`--force`면 다시 받는다).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import miniaudio
import numpy as np

from f1re.config import ConfigError, load_config, resolve_asset_path
from f1re.tts.radiofx import QWEN_SAMPLE_RATE, pcm_to_wav

# 효과음 사이트가 브라우저가 아닌 요청을 막는 경우가 있어 일반 브라우저 문자열을 쓴다.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TARGET_SAMPLE_RATE = QWEN_SAMPLE_RATE
SILENCE_THRESHOLD = 0.01


@dataclass(frozen=True)
class DecodedIntro:
    """디코딩·무음 제거를 마친 효과음."""

    pcm: bytes
    sample_rate: int
    source_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class FetchResult:
    """받아 오기 결과. 이미 있으면 `downloaded=False`."""

    path: Path
    downloaded: bool
    source_bytes: int = 0
    source_seconds: float = 0.0
    duration_seconds: float = 0.0


def decode_intro_audio(
    data: bytes,
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    threshold: float = SILENCE_THRESHOLD,
) -> DecodedIntro:
    """오디오 바이트를 24kHz 16bit mono PCM으로 디코딩하고 앞뒤 무음을 자른다."""
    try:
        decoded = miniaudio.decode(
            data,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=sample_rate,
        )
    except miniaudio.DecodeError as exc:
        raise ValueError("오디오를 디코딩하지 못했습니다") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("오디오를 디코딩하지 못했습니다") from exc

    samples = np.asarray(decoded.samples, dtype="<i2")
    if samples.size == 0:
        raise ValueError("효과음이 비어 있습니다")
    normalized = samples.astype(np.float64) / 32768.0
    active = np.flatnonzero(np.abs(normalized) >= threshold)
    if active.size == 0:
        raise ValueError("효과음에서 소리를 찾지 못했습니다")

    trimmed = samples[active[0] : active[-1] + 1]
    return DecodedIntro(
        pcm=trimmed.astype("<i2").tobytes(),
        sample_rate=sample_rate,
        source_seconds=samples.size / sample_rate,
        duration_seconds=trimmed.size / sample_rate,
    )


def download_audio(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    user_agent: str = BROWSER_USER_AGENT,
    timeout_s: float = 30.0,
) -> bytes:
    """효과음 원본을 내려받는다. User-Agent는 일반 브라우저 문자열을 쓴다."""
    with httpx.Client(
        timeout=timeout_s, follow_redirects=True, transport=transport
    ) as client:
        response = client.get(url, headers={"User-Agent": user_agent})
        response.raise_for_status()
        return response.content


def fetch_intro_sound(
    url: str,
    path: str | Path,
    *,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
    user_agent: str = BROWSER_USER_AGENT,
    timeout_s: float = 30.0,
) -> FetchResult:
    """효과음을 받아 WAV로 저장한다. 이미 있으면 `force`가 아닌 한 건너뛴다."""
    target = Path(path)
    if target.is_file() and not force:
        return FetchResult(path=target, downloaded=False)

    data = download_audio(
        url, transport=transport, user_agent=user_agent, timeout_s=timeout_s
    )
    decoded = decode_intro_audio(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pcm_to_wav(decoded.pcm, decoded.sample_rate))
    return FetchResult(
        path=target,
        downloaded=True,
        source_bytes=len(data),
        source_seconds=decoded.source_seconds,
        duration_seconds=decoded.duration_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="무전 시작 효과음을 받아 assets/에 저장한다."
    )
    parser.add_argument(
        "--force", action="store_true", help="이미 있어도 다시 받는다"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"설정을 읽지 못했습니다: {exc}")
        return 2

    path = resolve_asset_path(config.tts.intro_sound_path)
    try:
        result = fetch_intro_sound(
            config.tts.intro_sound_url, path, force=args.force
        )
    except (httpx.HTTPError, ValueError, OSError) as exc:
        print(f"효과음을 받지 못했습니다: {exc}")
        return 1

    if not result.downloaded:
        print(f"이미 있습니다: {result.path} (다시 받으려면 --force)")
        return 0
    print(
        f"저장: {result.path} "
        f"(원본 {result.source_bytes} bytes, {result.source_seconds:.2f}s → "
        f"무음 제거 {result.duration_seconds:.2f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

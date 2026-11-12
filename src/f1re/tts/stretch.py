"""WSOLA 시간 늘이기: 음 높이는 유지하고 말 속도만 바꾼다.

Qwen TTS는 말 속도 파라미터를 주지 않으므로 서버에서 합성 음성을 늘이거나
줄인다. 창 30ms(한 창 = 2 hop), 출력 hop = 창/2, 입력 hop = 출력 hop × 속도로
두고, 다음 조각의 시작을 앞 조각 꼬리와 상관이 가장 큰 곳(±8ms)으로 맞춘다.
속도 1.0이면 입력을 그대로 돌려준다.
"""

from __future__ import annotations

import numpy as np

from f1re.tts.radiofx import pcm_to_wav, wav_to_pcm

# 창 길이와 탐색 범위. PL 시제품과 같은 값이다.
FRAME_MS = 30.0
SEARCH_MS = 8.0


def time_stretch_pcm(pcm: bytes, sample_rate: int, speed: float) -> bytes:
    """16bit mono PCM의 길이를 `1/speed`로 늘이거나 줄인다. 음 높이는 유지한다."""
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("sample_rate는 양의 정수여야 합니다")
    if not isinstance(speed, (int, float)) or not np.isfinite(speed) or speed <= 0:
        raise ValueError("speed는 0보다 큰 유한한 수여야 합니다")
    if len(pcm) % 2 != 0:
        raise ValueError("16bit PCM 길이는 짝수여야 합니다")
    if not pcm:
        return b""
    if speed == 1.0:
        return pcm

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    stretched = _wsola(samples, sample_rate, float(speed))
    output = np.clip(np.round(stretched), -32768.0, 32767.0).astype("<i2")
    return output.tobytes()


def time_stretch_wav(wav_bytes: bytes, speed: float) -> bytes:
    """WAV(16bit mono) 바이트를 읽어 같은 형식의 늘인 WAV 바이트로 돌려준다."""
    pcm, sample_rate = wav_to_pcm(wav_bytes)
    return pcm_to_wav(time_stretch_pcm(pcm, sample_rate, speed), sample_rate)


def _wsola(signal: np.ndarray, sample_rate: int, speed: float) -> np.ndarray:
    frame = max(2, int(round(sample_rate * FRAME_MS / 1000.0)))
    hop_out = frame // 2
    hop_in = hop_out * speed
    search = max(1, int(round(sample_rate * SEARCH_MS / 1000.0)))
    overlap = frame - hop_out

    if signal.size < frame:
        # 창 하나보다 짧으면 늘이거나 줄일 재료가 없다.
        return signal.copy()

    max_start = signal.size - frame
    frame_count = int(np.floor(max_start / hop_in)) + 1
    windows = np.lib.stride_tricks.sliding_window_view(signal, overlap)
    window = np.hanning(frame)

    output = np.zeros((frame_count - 1) * hop_out + frame, dtype=np.float64)
    previous_start = 0
    for index in range(frame_count):
        nominal = int(round(index * hop_in))
        if index == 0:
            start = 0
        else:
            start = _best_continuation(
                signal,
                windows,
                nominal=nominal,
                previous_start=previous_start,
                hop_out=hop_out,
                max_start=max_start,
                search=search,
            )
        output[index * hop_out : index * hop_out + frame] += (
            signal[start : start + frame] * window
        )
        previous_start = start
    return output


def _best_continuation(
    signal: np.ndarray,
    windows: np.ndarray,
    *,
    nominal: int,
    previous_start: int,
    hop_out: int,
    max_start: int,
    search: int,
) -> int:
    """앞 조각 꼬리와 가장 잘 이어지는 시작점을 ±search 안에서 고른다."""
    target_start = previous_start + hop_out
    target = signal[target_start : target_start + hop_out]
    if target.size < hop_out:
        return int(np.clip(nominal, 0, max_start))

    candidates = np.clip(
        nominal + np.arange(-search, search + 1), 0, min(max_start, windows.shape[0] - 1)
    )
    segments = windows[candidates]
    target_energy = float(np.dot(target, target))
    if target_energy <= 0.0:
        return int(np.clip(nominal, 0, max_start))

    correlation = segments @ target
    energy = np.einsum("ij,ij->i", segments, segments)
    score = correlation / (np.sqrt(energy * target_energy) + 1e-12)
    return int(candidates[int(np.argmax(score))])

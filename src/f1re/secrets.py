"""Qwen API 키를 공용 키 도우미에서 읽는다.

레포 코드는 시크릿 저장 파일을 직접 읽지 않는다. 사용자 공용 설정에 있는
`qwen-key` 도우미 스크립트를 서브프로세스로 실행해 JSON을 파이프로 받고,
값은 메모리에만 둔다. 키 값은 로그·예외 메시지·repr에 넣지 않는다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# 공용 qwen-key 도우미. 표준출력으로 {"api_key": ..., "base_url": ...}을 낸다.
DEFAULT_KEY_HELPER = Path.home() / ".claude" / "skills" / "qwen-key" / "get-qwen-key.py"


class SecretsError(Exception):
    """키 도우미를 찾지 못했거나 값이 비어 있을 때. 값 자체는 담지 않는다."""


def _run_key_helper(command: Sequence[str]) -> Any:
    """도우미 명령을 실행하고 표준출력 JSON을 돌려준다. 실패 내용은 감춘다."""
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretsError("Qwen 키 도우미를 실행하지 못했습니다") from exc
    if completed.returncode != 0:
        # stderr에는 값이 섞일 수 있으므로 메시지에 넣지 않는다.
        raise SecretsError(f"Qwen 키 도우미가 실패했습니다 (종료 코드 {completed.returncode})")
    output = completed.stdout.strip()
    if not output:
        raise SecretsError("Qwen 키 도우미가 빈 값을 반환했습니다")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise SecretsError("Qwen 키 도우미 출력이 JSON이 아닙니다") from exc


def _extract_api_key(value: Any, secret_key: str) -> str:
    """도우미 결과가 문자열이거나 {api_key, base_url?} dict인 경우를 모두 처리한다.

    `base_url`은 OpenAI 호환 주소일 수 있으므로 쓰지 않는다. TTS 엔드포인트는
    `config/default.toml`의 `tts.endpoint`를 쓴다.
    """
    if isinstance(value, str):
        key = value.strip()
    elif isinstance(value, Mapping):
        raw_key = value.get("api_key")
        key = raw_key.strip() if isinstance(raw_key, str) else ""
    else:
        key = ""
    if not key:
        raise SecretsError(f"시크릿 '{secret_key}'에 api_key가 없습니다")
    return key


def _default_command(secret_key: str, helper: Path) -> list[str]:
    if secret_key != "qwen":
        raise SecretsError(f"이 도우미는 qwen 항목만 읽습니다 (요청: '{secret_key}')")
    return [sys.executable, "-I", str(helper)]


def load_qwen_key(secret_key: str = "qwen", *, helper: Path | None = None) -> str:
    """Qwen API 키를 읽는다. 키 문자열 또는 dict(`api_key`)를 모두 받는다."""
    command = _default_command(secret_key, helper or DEFAULT_KEY_HELPER)
    value = _run_key_helper(command)
    return _extract_api_key(value, secret_key)

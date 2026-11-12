"""키 도우미 연동. 실제 시크릿 모듈은 부르지 않는다."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import f1re.secrets as secrets

FAKE_KEY = "test-key-not-real"


def test_load_qwen_key_accepts_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets, "_run_key_helper", lambda command: FAKE_KEY)

    assert secrets.load_qwen_key() == FAKE_KEY


def test_load_qwen_key_accepts_dict_with_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"api_key": FAKE_KEY, "base_url": "https://example.invalid/v1"}
    monkeypatch.setattr(secrets, "_run_key_helper", lambda command: payload)

    assert secrets.load_qwen_key() == FAKE_KEY


def test_missing_api_key_raises_without_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        secrets, "_run_key_helper", lambda command: {"api_key": 12345}
    )

    with pytest.raises(secrets.SecretsError) as caught:
        secrets.load_qwen_key()

    assert "api_key" in str(caught.value)
    assert "12345" not in str(caught.value)


def test_non_qwen_secret_name_is_rejected() -> None:
    with pytest.raises(secrets.SecretsError, match="qwen"):
        secrets.load_qwen_key("openai")


def test_key_helper_outputs_json_without_key_in_command(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import json\n"
        f"print(json.dumps({{'api_key': '{FAKE_KEY}', "
        "'base_url': 'https://example.invalid/v1'}))\n",
        encoding="utf-8",
    )

    payload: Any = secrets._run_key_helper(
        [sys.executable, "-I", str(helper)]
    )

    assert payload == {
        "api_key": FAKE_KEY,
        "base_url": "https://example.invalid/v1",
    }


def test_key_helper_failure_hides_stderr(tmp_path: Path) -> None:
    marker = "do-not-leak-this-value"
    helper = tmp_path / "helper.py"
    helper.write_text(
        f"import sys\nsys.stderr.write('{marker}')\nraise SystemExit(3)\n",
        encoding="utf-8",
    )

    with pytest.raises(secrets.SecretsError) as caught:
        secrets._run_key_helper([sys.executable, "-I", str(helper)])

    assert "종료 코드 3" in str(caught.value)
    assert marker not in str(caught.value)

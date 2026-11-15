"""녹화 목록·다운로드·웹 다시 보기 API 라우터(설계 4.5·4.6, 계획 작업 12).

`web/app.py`가 앱 수명에 한 줄로 붙이는 라우터다. 엔진 ON/OFF와 무관하게
동작해야 하므로(설계 4.5), 이 모듈은 `Engine`·실시간 상태를 건드리지 않고
`session_recordings` DB와 리플레이 전용 객체만 쓴다.

- ``GET /api/recordings``: 최신순 녹화 목록. 범주 필터·개수 제한, 화면 배지용
  범주/종료 사유 이름과 파일 보유 여부(`downloadable`)를 함께 내려 준다.
- ``GET /api/recordings/{id}/download``: 로컬 파일이 있으면 그대로, 없으면
  `RecordingFetcher`가 GCS에서 `.cache/`로 복원해 `.f1rec.zst`를 내려 준다.
  같은 녹화의 동시 다운로드·재생 요청은 `RecordingFetcher`의 잠금이 합친다.
- ``POST /api/recordings/{id}/retry-backup``: `BackupWorker`에 수동 재시도를 맡긴다.
- ``POST /api/replay/start``·``/api/replay/control``: `ReplayManager`로 재생을
  시작하고 일시정지·재개·시크·배속·정지를 제어한다. 시크의 전이 기억 초기화와
  지연 없는 재구성은 `ReplayManager.seek()`가 맡는다(계획 Review Focus 5).
- ``GET /api/replay/state``·``WS /ws/replay``: 실시간 `/api/state`와 같은 구조에
  `replay` 메타를 더한 상태를 10Hz로 보낸다.
- ``GET /api/replay/track``·``/api/replay/report``·``/api/replay/radio/log``:
  리플레이 중인 세션의 지도·리포트·무전 기록. 기존 `map.js`·`report.js`가 그대로
  읽는 모양이라 화면 부품을 다시 만들지 않는다.

리플레이는 `ReplayManager`가 메모리 전용 `StateStore`·`RacePipeline(conn=None)`·
`AlertRules`·`MemoryRadioSink`로 돌리므로, 이 라우터가 하는 DB 작업은 읽기뿐이고
Qwen TTS 호출도 일어나지 않는다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from f1re import db
from f1re.config import resolve_asset_path
from f1re.tracks import TrackDataError, load_track
from f1re.udp.gcs_transfer import (
    BackupError,
    BackupSkippedError,
    RecordingFetcher,
    RecordingNotFoundError,
)
from f1re.udp.session_category import category_label, end_reason_label
from f1re.udp.web_replay import DEFAULT_RADIO_LIMIT, ReplayManager

# 목록 API 기본·최대 개수. 화면은 기본 50개를 요청한다(동결 API 계약).
DEFAULT_RECORDINGS_LIMIT = 50
MAX_RECORDINGS_LIMIT = 200

# `WS /ws/replay` 상태 푸시 주기(초). 실시간 `/ws/state`와 같은 10Hz다.
REPLAY_PUSH_INTERVAL_S = 0.1

# 무전 기록 API의 최대 줄 수. 기존 `/api/radio/log`와 같은 상한이다.
MAX_RADIO_LOG_LIMIT = 200

# 다운로드 응답의 미디어 타입. `.f1rec.zst`(zstd) 원본 파일이다.
DOWNLOAD_MEDIA_TYPE = "application/zstd"


class RetryWorker(Protocol):
    """`BackupWorker` 중 이 라우터가 쓰는 부분(수동 재시도)."""

    async def retry(self, recording_id: int) -> dict[str, Any]:
        """녹화 하나의 업로드를 즉시 다시 시도하고 결과 요약을 돌려준다."""


class ReplayStartRequest(BaseModel):
    """`POST /api/replay/start` 요청(동결 API 계약)."""

    recording_id: int
    speed: float = 1.0


class ReplayControlRequest(BaseModel):
    """`POST /api/replay/control` 요청. `action`마다 쓰는 필드가 다르다."""

    action: str
    speed: float | None = None
    position_ms: int | None = None


def create_replay_router(
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    fetcher: RecordingFetcher,
    replay_manager: ReplayManager,
    backup_worker: RetryWorker | None = None,
) -> APIRouter:
    """녹화·리플레이 라우터를 만든다. `web/app.py`가 앱에 붙인다.

    `conn_factory`는 호출마다 새 SQLite 연결을 여는 공장이다(이 라우터가 읽고
    닫는다). `fetcher`는 로컬 우선·GCS 온디맨드 복원·중복 다운로드 병합을
    맡고, `replay_manager`는 메모리 전용 리플레이를 돌린다. `backup_worker`는
    수동 백업 재시도 API에만 쓰며 없으면 그 API가 503으로 답한다.
    """
    router = APIRouter()

    # ------------------------------------------------------------------
    # 녹화 목록·다운로드

    @router.get("/api/recordings")
    def api_recordings(
        category: str | None = Query(default=None, description="녹화 범주"),
        limit: int = Query(
            default=DEFAULT_RECORDINGS_LIMIT,
            ge=1,
            le=MAX_RECORDINGS_LIMIT,
            description="최대 개수",
        ),
    ) -> dict[str, Any]:
        """최신순 녹화 목록. 화면 배지·보관 위치·다운로드 가능 여부를 함께 준다."""
        rows = _list_recording_rows(
            conn_factory, category=_clean_category(category), limit=limit
        )
        track_names = _track_names(rows)
        return {
            "recordings": [
                recording_payload(row, track_names.get(_as_int(row.get("track_id"))))
                for row in rows
            ]
        }

    @router.get("/api/recordings/{recording_id}/download")
    async def api_recording_download(recording_id: int) -> FileResponse:
        """녹화 파일을 내려 준다. 로컬에 없으면 GCS에서 복원해 전송한다."""
        # 재생 루프와 같은 이벤트 루프를 쓰므로 DB 읽기는 스레드풀에 맡긴다.
        row = await asyncio.to_thread(
            _load_recording, conn_factory, recording_id
        )
        if row is None:
            raise _not_found(recording_id)
        if not _has_download_source(row):
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "로컬 파일도 클라우드 백업도 없는 녹화입니다",
                    "recording_id": int(recording_id),
                },
            )
        path = await _ensure_local_file(fetcher, recording_id)
        if not path.is_file():
            raise _not_found(recording_id)
        return FileResponse(
            path,
            media_type=DOWNLOAD_MEDIA_TYPE,
            filename=_download_name(row, path),
        )

    @router.post("/api/recordings/{recording_id}/retry-backup")
    async def api_recording_retry_backup(recording_id: int) -> dict[str, Any]:
        """업로드가 남은 녹화의 백업을 즉시 다시 시도한다."""
        if backup_worker is None:
            raise HTTPException(
                status_code=503,
                detail={"message": "백업 워커가 없습니다"},
            )
        result = await backup_worker.retry(int(recording_id))
        if not isinstance(result, dict):
            result = {"recording_id": int(recording_id), "result": result}
        if result.get("backup_status") is None:
            # `BackupWorker.retry()`는 없는 녹화를 상태 `None`으로 알린다.
            raise _not_found(recording_id)
        return result

    # ------------------------------------------------------------------
    # 리플레이 시작·제어

    @router.post("/api/replay/start")
    async def api_replay_start(body: ReplayStartRequest) -> dict[str, Any]:
        """녹화를 확보(로컬 또는 GCS 복원)하고 리플레이를 시작한다."""
        recording_id = int(body.recording_id)
        row = await asyncio.to_thread(
            _load_recording, conn_factory, recording_id
        )
        if row is None:
            raise _not_found(recording_id)
        if _is_recording_in_progress(row):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "아직 녹화가 끝나지 않은 세션입니다",
                    "recording_id": recording_id,
                },
            )
        path = await _ensure_local_file(fetcher, recording_id)
        try:
            return await replay_manager.start(row, path, speed=body.speed)
        except FileNotFoundError as exc:
            raise _not_found(recording_id) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/replay/control")
    async def api_replay_control(body: ReplayControlRequest) -> dict[str, Any]:
        """일시정지·재개·시크·배속·정지. 결과는 최신 리플레이 상태 묶음이다."""
        action = body.action.strip().lower()
        if action == "pause":
            return await replay_manager.pause()
        if action == "resume":
            return await replay_manager.resume()
        if action == "seek":
            if body.position_ms is None:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "seek에는 position_ms가 필요합니다"},
                )
            return await replay_manager.seek(int(body.position_ms))
        if action == "speed":
            if body.speed is None:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "speed에는 speed 값이 필요합니다"},
                )
            try:
                return await replay_manager.set_speed(body.speed)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if action == "stop":
            await replay_manager.stop()
            return replay_manager.state_payload()
        raise HTTPException(
            status_code=400,
            detail={
                "message": "action은 pause·resume·seek·speed·stop 중 하나여야 합니다",
                "action": body.action,
            },
        )

    # ------------------------------------------------------------------
    # 리플레이 상태·화면 데이터

    # 아래 조회 API는 `async def`다. `ReplayManager`는 재생 태스크와 같은
    # 이벤트 루프에서 상태를 바꾸므로, 스레드풀(sync 엔드포인트)이 아니라 루프
    # 위에서 읽어 일관된 시점의 스냅샷을 돌려준다.

    @router.get("/api/replay/state")
    async def api_replay_state() -> dict[str, Any]:
        """실시간 `/api/state`와 같은 구조 + `replay` 메타."""
        return replay_manager.state_payload()

    @router.get("/api/replay/track")
    async def api_replay_track() -> dict[str, Any]:
        """리플레이 중인 세션의 서킷 정보. `map.js`가 그대로 읽는다."""
        return replay_manager.track_payload()

    @router.get("/api/replay/report")
    async def api_replay_report() -> dict[str, Any]:
        """리플레이 중인 세션의 코너 리포트. `report.js`가 그대로 읽는다."""
        return replay_manager.report_payload()

    @router.get("/api/replay/radio/log")
    async def api_replay_radio_log(
        limit: int = Query(
            default=DEFAULT_RADIO_LIMIT,
            ge=1,
            le=MAX_RADIO_LOG_LIMIT,
            description="최대 줄 수",
        ),
    ) -> dict[str, Any]:
        """리플레이 전용 메모리 무전 기록(DB의 실세션 기록과 무관)."""
        return replay_manager.radio_log_payload(limit=limit)

    @router.websocket("/ws/replay")
    async def ws_replay(websocket: WebSocket) -> None:
        """리플레이 상태를 10Hz로 민다. 리플레이가 없으면 비활성 메타를 보낸다."""
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(replay_manager.state_payload())
                await asyncio.sleep(REPLAY_PUSH_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # 브라우저가 닫았거나 서버가 종료 중이다. 정리는 Starlette가 한다.
            return

    return router


# ---------------------------------------------------------------------------
# 녹화 목록


def recording_payload(
    row: dict[str, Any], track_name: str | None = None
) -> dict[str, Any]:
    """`session_recordings` 행 하나를 화면이 바로 쓰는 모양으로 바꾼다.

    `local_exists`·`downloadable`은 DB 플래그가 아니라 **지금 실제 파일이
    있는지**로 판정한다. 백업 워커가 정리한 뒤 DB 갱신이 늦었거나 반대로
    파일만 남은 경우에도 화면의 "로컬/클라우드" 표시와 다운로드 버튼이 실제
    동작과 어긋나지 않게 하기 위해서다.
    """
    local_file = _existing_local_file(row.get("local_path"))
    has_remote = bool(row.get("gcs_object"))
    return {
        "id": int(row["id"]),
        "session_uid": _as_int(row.get("session_uid")),
        "segment": _as_int(row.get("segment")) or 1,
        "end_reason": row.get("end_reason"),
        "end_reason_label": end_reason_label(_as_str(row.get("end_reason"))),
        "category": row.get("category"),
        "category_label": category_label(_as_str(row.get("category")) or ""),
        "session_type": _as_int(row.get("session_type")),
        "track_id": _as_int(row.get("track_id")),
        "track_name": track_name,
        "started_at_ns": _as_int(row.get("started_at_ns")),
        "duration_ms": _as_int(row.get("duration_ms")) or 0,
        "packet_count": _as_int(row.get("packet_count")) or 0,
        "file_size_bytes": _as_int(row.get("file_size_bytes")),
        "local_exists": local_file is not None,
        "backup_status": row.get("backup_status"),
        "downloadable": local_file is not None or has_remote,
    }


def _list_recording_rows(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    category: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """녹화 목록을 읽는다. DB를 못 읽으면 503으로 알린다."""
    try:
        conn = conn_factory()
    except sqlite3.Error as exc:
        raise _db_unavailable(exc) from exc
    try:
        return db.list_recordings(conn, category=category, limit=limit)
    except sqlite3.Error as exc:
        raise _db_unavailable(exc) from exc
    finally:
        conn.close()


def _track_names(rows: list[dict[str, Any]]) -> dict[int, str | None]:
    """행들의 서킷 ID → 이름 표. 같은 ID는 한 번만 조회한다.

    서킷 데이터 파일이 없는 ID(사용자 정의 트랙 등)는 `None`으로 두어 화면이
    "서킷 <ID>"로 대신 보여 준다.
    """
    names: dict[int, str | None] = {}
    for row in rows:
        track_id = _as_int(row.get("track_id"))
        if track_id is None or track_id in names:
            continue
        try:
            track = load_track(track_id)
        except (TrackDataError, OSError):
            track = None
        names[track_id] = None if track is None else track.name
    return names


# ---------------------------------------------------------------------------
# 복원·다운로드


async def _ensure_local_file(
    fetcher: RecordingFetcher, recording_id: int
) -> Path:
    """로컬 또는 GCS 복원 캐시 경로. 실패 사유를 HTTP 상태로 바꾼다.

    다운로드와 리플레이 시작이 같은 `RecordingFetcher`를 쓰므로, 같은 녹화에
    대한 동시 요청은 fetcher 안의 잠금이 중복 `rclone` 호출을 합친다(설계 4.5).
    """
    try:
        return await fetcher.ensure_local_file(int(recording_id))
    except RecordingNotFoundError as exc:
        raise _not_found(recording_id) from exc
    except BackupSkippedError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "rclone 또는 GCS 설정이 없어 녹화를 복원할 수 없습니다",
                "recording_id": int(recording_id),
            },
        ) from exc
    except BackupError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"녹화 복원에 실패했습니다: {exc}",
                "recording_id": int(recording_id),
            },
        ) from exc


def _has_download_source(row: dict[str, Any]) -> bool:
    """로컬 파일이나 GCS 객체 중 하나라도 있어 내려 줄 수 있는가."""
    return (
        _existing_local_file(row.get("local_path")) is not None
        or bool(row.get("gcs_object"))
    )


def _download_name(row: dict[str, Any], path: Path) -> str:
    """`Content-Disposition`에 쓸 파일 이름. 경로 구분자는 떼어 낸다."""
    for candidate in (row.get("local_path"), row.get("gcs_object")):
        if candidate:
            name = Path(str(candidate)).name
            if name:
                return name
    return path.name


def _existing_local_file(raw_path: Any) -> Path | None:
    """DB의 `local_path`가 실제 파일이면 절대 경로로 돌려준다.

    레포 기준 상대 경로(설계 4.4)는 레포 루트에서 편다. 파일이 없으면 `None`.
    """
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = resolve_asset_path(path)
    return path if path.is_file() else None


# ---------------------------------------------------------------------------
# DB·오류 도우미


def _load_recording(
    conn_factory: Callable[[], sqlite3.Connection], recording_id: int
) -> dict[str, Any] | None:
    try:
        conn = conn_factory()
    except sqlite3.Error as exc:
        raise _db_unavailable(exc) from exc
    try:
        return db.get_recording(conn, int(recording_id))
    except sqlite3.Error as exc:
        raise _db_unavailable(exc) from exc
    finally:
        conn.close()


def _is_recording_in_progress(row: dict[str, Any]) -> bool:
    """아직 마감·압축되지 않은 녹화인가(재생·다운로드 대상이 아니다)."""
    return (
        row.get("end_reason") == "recording"
        or row.get("backup_status") == "recording"
    )


def _clean_category(category: str | None) -> str | None:
    """빈 문자열 범주는 "전체"로 본다(화면 필터의 기본값)."""
    if category is None:
        return None
    text = category.strip()
    return text or None


def _db_unavailable(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"message": f"녹화 DB를 읽지 못했습니다: {exc}"},
    )


def _not_found(recording_id: int) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "message": f"녹화 {int(recording_id)}를 찾을 수 없습니다",
            "recording_id": int(recording_id),
        },
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "DEFAULT_RECORDINGS_LIMIT",
    "DOWNLOAD_MEDIA_TYPE",
    "MAX_RECORDINGS_LIMIT",
    "ReplayControlRequest",
    "ReplayStartRequest",
    "REPLAY_PUSH_INTERVAL_S",
    "RetryWorker",
    "create_replay_router",
    "recording_payload",
]

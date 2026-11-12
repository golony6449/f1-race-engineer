"""F1 25 UDP 패킷 규격의 공용 이름.

지원 형식은 2025 하나다(`packetFormat` 2025). 형식이 늘면 `formats/` 아래에
모듈을 추가하고 `FORMATS`에 등록한다. `packetFormat`이 표에 없으면 파서가
`UnsupportedPacket("format")`으로 거부한다(2026 Season Pack 포함).

패킷 구조체 자체는 `formats/f2025.py`에 있고, 이 모듈은 헤더·패킷 ID·공식
바이트 수·송신 주기처럼 형식과 무관하게 쓰는 이름을 둔다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from types import ModuleType

# 공통 헤더 29바이트. 리틀 엔디언, packed.
# uint16, uint8×4, uint64, float, uint32×2, uint8×2
HEADER_STRUCT = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER_STRUCT.size


class PacketId(IntEnum):
    """공식 문서의 PacketId 열거형."""

    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    FINAL_CLASSIFICATION = 8
    LOBBY_INFO = 9
    CAR_DAMAGE = 10
    SESSION_HISTORY = 11
    TYRE_SETS = 12
    MOTION_EX = 13
    TIME_TRIAL = 14
    LAP_POSITIONS = 15


@dataclass(frozen=True)
class Header:
    """공통 패킷 헤더. 필드 순서는 공식 PacketHeader 구조체와 같다."""

    packet_format: int
    game_year: int
    game_major: int
    game_minor: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_id: int
    overall_frame_id: int
    player_car_index: int
    secondary_player_car_index: int

    def pack(self) -> bytes:
        return HEADER_STRUCT.pack(
            self.packet_format,
            self.game_year,
            self.game_major,
            self.game_minor,
            self.packet_version,
            self.packet_id,
            self.session_uid,
            self.session_time,
            self.frame_id,
            self.overall_frame_id,
            self.player_car_index,
            self.secondary_player_car_index,
        )

    @classmethod
    def unpack_from(cls, data: bytes, offset: int = 0) -> Header:
        """헤더 29바이트를 읽는다. `data`가 짧으면 `struct.error`가 난다."""
        return cls(*HEADER_STRUCT.unpack_from(data, offset))


PACKET_FORMAT = 2025

# 공식 "F1 25 Telemetry Output Structures.txt"의 패킷별 전체 바이트 수(헤더 포함).
# 정의 크기 합이 이 값과 같은지는 tests/udp/test_spec.py가 확인한다.
PACKET_SIZES: dict[PacketId, int] = {
    PacketId.MOTION: 1349,
    PacketId.SESSION: 753,
    PacketId.LAP_DATA: 1285,
    PacketId.EVENT: 45,
    PacketId.PARTICIPANTS: 1284,
    PacketId.CAR_SETUPS: 1133,
    PacketId.CAR_TELEMETRY: 1352,
    PacketId.CAR_STATUS: 1239,
    PacketId.FINAL_CLASSIFICATION: 1042,
    PacketId.LOBBY_INFO: 954,
    PacketId.CAR_DAMAGE: 1041,
    PacketId.SESSION_HISTORY: 1460,
    PacketId.TYRE_SETS: 231,
    PacketId.MOTION_EX: 273,
    PacketId.TIME_TRIAL: 101,
    PacketId.LAP_POSITIONS: 1131,
}

# "메뉴 설정 주기" 패킷의 기본값. 게임 설정에서 바뀔 수 있으므로 데모 송신기는
# 실행 인자로 덮어쓸 수 있게 한다.
MENU_RATE_HZ = 60.0

# 공식 문서의 패킷 송신 주기(Hz). None은 주기 송신이 아니라 이벤트가 생길 때만
# 보내는 패킷이다.
PACKET_RATES_HZ: dict[PacketId, float | None] = {
    PacketId.MOTION: MENU_RATE_HZ,
    PacketId.SESSION: 2.0,
    PacketId.LAP_DATA: MENU_RATE_HZ,
    PacketId.EVENT: None,
    PacketId.PARTICIPANTS: 0.2,  # 5초마다
    PacketId.CAR_SETUPS: 2.0,
    PacketId.CAR_TELEMETRY: MENU_RATE_HZ,
    PacketId.CAR_STATUS: MENU_RATE_HZ,
    PacketId.FINAL_CLASSIFICATION: None,
    PacketId.LOBBY_INFO: 2.0,
    PacketId.CAR_DAMAGE: 10.0,
    PacketId.SESSION_HISTORY: 20.0,  # 차를 돌아가며
    PacketId.TYRE_SETS: 20.0,  # 차를 돌아가며
    PacketId.MOTION_EX: MENU_RATE_HZ,
    PacketId.TIME_TRIAL: 1.0,
    PacketId.LAP_POSITIONS: 1.0,
}

# 형식별 정의 모듈. 모듈은 FORMAT, PACKET_SIZES, PACKETS를 제공해야 한다.
# 순환 import: formats/f2025.py가 이 모듈의 PacketId를 쓰므로 맨 아래에서 가져온다.
from f1re.udp.formats import f2025  # noqa: E402

FORMATS: dict[int, ModuleType] = {PACKET_FORMAT: f2025}

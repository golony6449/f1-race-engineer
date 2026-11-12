"""UDP 패킷 구조체를 선언하는 도구.

EA 공식 구조체 파일의 필드 순서·형·배열 길이를 그대로 옮겨 적을 수 있게
필드 정의(`ScalarField` 등)와 구조체(`StructDef`)를 제공한다. 파서와 패커가
이 정의 하나를 함께 쓰므로 왕복이 어긋날 수 없다.

리틀 엔디언 packed로 컴파일한다. `struct` 모듈은 정렬 패딩을 넣지 않으므로
공식 문서가 말하는 packed 구조체와 같은 바이트 수가 나온다.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")

# Event 패킷 본문에서 쓰는 키 이름.
EVENT_CODE_KEY = "event_string_code"
EVENT_DETAILS_KEY = "event_details"


def snake_case(name: str) -> str:
    """공식 문서 필드 이름(m_ 접두어, camelCase)을 snake_case로 바꾼다."""
    if name.startswith("m_"):
        name = name[2:]
    name = _ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    name = _CAMEL_BOUNDARY.sub(r"\1_\2", name)
    return name.lower()


@dataclass(frozen=True)
class _Named:
    """공식 문서 필드 이름을 받아 body 키(snake_case)를 미리 만들어 둔다."""

    name: str
    key: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", snake_case(self.name))


@dataclass(frozen=True)
class ScalarField(_Named):
    """단일 값 또는 같은 형의 1차원 배열. count=1이면 단일 값이다."""

    fmt: str
    count: int = 1


@dataclass(frozen=True)
class StringField(_Named):
    """char 배열. UTF-8로 풀고 끝의 NUL을 뗀 str로 다룬다."""

    length: int


@dataclass(frozen=True)
class RawField(_Named):
    """해석하지 않고 bytes 그대로 두는 고정 길이 영역."""

    length: int


@dataclass(frozen=True)
class MatrixField(_Named):
    """2차원 숫자 배열(예: 랩별 차 순위). list of list로 다룬다."""

    fmt: str
    rows: int
    cols: int


@dataclass(frozen=True)
class StructField(_Named):
    """다른 구조체 하나 또는 그 배열. count=None이면 단일 구조체다."""

    struct: StructDef
    count: int | None = None


Field = ScalarField | StringField | RawField | MatrixField | StructField


def _field_formats(field_def: Field) -> list[str]:
    """필드 하나가 펼치는 struct 형식 문자들을 낸다."""
    if isinstance(field_def, ScalarField):
        return [field_def.fmt] * field_def.count
    if isinstance(field_def, StringField):
        return [f"{field_def.length}s"]
    if isinstance(field_def, RawField):
        return [f"{field_def.length}s"]
    if isinstance(field_def, MatrixField):
        return [field_def.fmt] * (field_def.rows * field_def.cols)
    nested = [fmt for child in field_def.struct.fields for fmt in _field_formats(child)]
    return nested * (1 if field_def.count is None else field_def.count)


@dataclass(frozen=True)
class StructDef:
    """필드 목록으로 만든 packed 구조체. 파싱·패킹 방법을 함께 가진다."""

    fields: tuple[Field, ...]
    _compiled: struct.Struct = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        keys = [field_def.key for field_def in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError(f"중복된 필드 이름: {keys}")
        formats = [fmt for field_def in self.fields for fmt in _field_formats(field_def)]
        object.__setattr__(self, "_compiled", struct.Struct("<" + "".join(formats)))

    @property
    def size(self) -> int:
        return self._compiled.size

    def unpack(self, data: bytes) -> dict[str, Any]:
        values = iter(self._compiled.unpack(data))
        return self._consume(values)

    def _consume(self, values: Iterator[Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for field_def in self.fields:
            if isinstance(field_def, ScalarField):
                chunk = [next(values) for _ in range(field_def.count)]
                body[field_def.key] = chunk[0] if field_def.count == 1 else chunk
            elif isinstance(field_def, StringField):
                raw = next(values)
                body[field_def.key] = raw.decode("utf-8", errors="replace").rstrip("\x00")
            elif isinstance(field_def, RawField):
                body[field_def.key] = next(values)
            elif isinstance(field_def, MatrixField):
                body[field_def.key] = [
                    [next(values) for _ in range(field_def.cols)]
                    for _ in range(field_def.rows)
                ]
            else:
                if field_def.count is None:
                    body[field_def.key] = field_def.struct._consume(values)
                else:
                    body[field_def.key] = [
                        field_def.struct._consume(values) for _ in range(field_def.count)
                    ]
        return body

    def pack(self, body: Mapping[str, Any]) -> bytes:
        return self._compiled.pack(*self._flatten(body))

    def _flatten(self, body: Mapping[str, Any]) -> Iterator[Any]:
        for field_def in self.fields:
            value = body[field_def.key]
            if isinstance(field_def, ScalarField):
                if field_def.count == 1:
                    yield value
                else:
                    yield from value
            elif isinstance(field_def, (StringField, RawField)):
                yield value.encode("utf-8") if isinstance(value, str) else value
            elif isinstance(field_def, MatrixField):
                for row in value:
                    yield from row
            elif field_def.count is None:
                yield from field_def.struct._flatten(value)
            else:
                for item in value:
                    yield from field_def.struct._flatten(item)


@dataclass(frozen=True)
class EventDetails:
    """Event 패킷 m_eventDetails union. 이벤트 문자열 코드로 해석한다."""

    size: int
    structs: Mapping[str, StructDef]
    empty_codes: frozenset[str]

    def unpack(self, code: str, raw: bytes) -> dict[str, Any]:
        struct_def = self.structs.get(code)
        if struct_def is not None:
            return struct_def.unpack(raw[: struct_def.size].ljust(struct_def.size, b"\x00"))
        if code in self.empty_codes:
            return {}
        return {"raw": raw}

    def pack(self, code: str, details: Mapping[str, Any]) -> bytes:
        struct_def = self.structs.get(code)
        if struct_def is not None:
            return struct_def.pack(details).ljust(self.size, b"\x00")
        if code in self.empty_codes:
            return bytes(self.size)
        raw = bytes(details["raw"])
        return raw[: self.size].ljust(self.size, b"\x00")


@dataclass(frozen=True)
class PacketDef:
    """패킷 하나의 본문 정의. Event만 union 처리가 붙는다."""

    packet_id: int
    name: str
    body: StructDef
    event: EventDetails | None = None

    def parse_body(self, payload: bytes) -> dict[str, Any]:
        body = self.body.unpack(payload)
        if self.event is not None:
            body[EVENT_DETAILS_KEY] = self.event.unpack(
                body[EVENT_CODE_KEY], body[EVENT_DETAILS_KEY]
            )
        return body

    def pack_body(self, body: Mapping[str, Any]) -> bytes:
        if self.event is None:
            return self.body.pack(body)
        values = dict(body)
        values[EVENT_DETAILS_KEY] = self.event.pack(
            body[EVENT_CODE_KEY], body[EVENT_DETAILS_KEY]
        )
        return self.body.pack(values)


def F(name: str, fmt: str) -> ScalarField:
    """단일 숫자 필드."""
    return ScalarField(name, fmt)


def FA(name: str, fmt: str, count: int) -> ScalarField:
    """같은 형의 1차원 배열 필드."""
    return ScalarField(name, fmt, count)


def S(name: str, length: int) -> StringField:
    """char 배열(문자열) 필드."""
    return StringField(name, length)


def RAW(name: str, length: int) -> RawField:
    """해석하지 않는 bytes 필드."""
    return RawField(name, length)


def M(name: str, fmt: str, rows: int, cols: int) -> MatrixField:
    """2차원 숫자 배열 필드."""
    return MatrixField(name, fmt, rows, cols)


def T(name: str, struct_def: StructDef) -> StructField:
    """단일 구조체 필드."""
    return StructField(name, struct_def)


def TA(name: str, struct_def: StructDef, count: int) -> StructField:
    """구조체 배열 필드."""
    return StructField(name, struct_def, count)

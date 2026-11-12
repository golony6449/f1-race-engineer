# f1-race-engineer

F1 25 UDP 텔레메트리 기반 한국어 AI 레이스 엔지니어.

- 설계: `docs/design/2026-11-12-korean-race-engineer-design.md`
- 계획: `docs/plan/2026-11-12-korean-race-engineer-plan.md` (작업 N ↔ Jira SCRUM 일감, 에픽 SCRUM-48)
- 환경은 uv만 쓴다(`uv add`, `uv run`). `pyproject.toml`과 `uv.lock`을 커밋한다.
- 서버는 리눅스. Windows 전용 코드 금지. 스크립트는 파이썬.
- 공개 레포다. 비밀값·키 값, 상용 앱 분석 자료는 넣지 않는다. 비밀값은 공용 시크릿 모듈(`~/.claude/bin/claude_secrets.py`)의 `get()`으로만 읽는다.
- 주석·문서·커밋 메시지는 한국어. 브랜치 `feature/SCRUM-<번호>`, PR 제목 앞에 `[SCRUM-<번호>]`.
- 테스트: `uv run pytest`

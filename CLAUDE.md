# obsidian-notion-sync

input/ 폴더의 마크다운 파일을 Notion으로 자동 동기화하는 파이프라인.

## 파이프라인 구조

```
input/ (날것 노트)
  → Claude Code가 주제별로 정리
  → status: done 추가
  → watch 모드가 output/ 저장 + Notion 업로드
```

## 파일 처리 규칙

`input/` 파일에 아래 frontmatter가 있을 때만 파이프라인이 실행됨:

```yaml
---
title: 제목
status: done        # 이게 있어야 처리됨
project: 프로젝트명  # 있으면 Projects DB, 없으면 Inbox
---
```

## 내가 해야 할 일 (Claude Code)

사용자가 `input/` 파일 정리를 요청하면:

1. 파일을 읽고 주제별로 분리
2. 주제가 하나면 → 원본 파일에 frontmatter만 추가
3. 주제가 여러 개면 → 주제별로 새 파일 생성, 원본은 삭제
4. 각 파일에 적절한 title, status: done 추가
5. project 필드는 사용자에게 확인 후 추가 (모르면 생략 → Inbox)

### 파일명 규칙
- 한국어 제목 그대로 사용 가능
- 공백 허용

### 예시

사용자: "input/메모.md 정리해줘"

원본 내용이 '에이전틱 루프'와 '세션 관리' 두 주제면:

```
input/에이전틱 루프.md  (status: done)
input/세션 관리.md      (status: done)
```

원본 `input/메모.md` 는 삭제.

## 파이프라인 실행

```bash
# watch 모드 (추천 - 파일 저장 시 자동 업로드)
uv run process.py --watch

# 수동 처리
uv run process.py "input/파일명.md"

# 노션 제목 변경사항 로컬로 가져오기
uv run process.py --pull
```

## 파일 구조

```
process.py     진입점, 파이프라인, 워처
notion_api.py  Notion API 호출
converter.py   마크다운 → Notion 블록 변환
input/         날것 노트 (git 미추적)
output/        처리된 파일 + .mappings/ (git 미추적)
```

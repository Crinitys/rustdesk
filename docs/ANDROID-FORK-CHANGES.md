# Android 포크 작업 정리 (`android-soft-keyboard-fix` 브랜치)

날짜: 2026-09-05
포크: [[reference_personal_rustdesk_fork]] (Crinitys/rustdesk)
테스트 기기: Galaxy S25 Ultra

이 브랜치에서 한 작업 전체 요약. 세부 설계/계획은 아래 각 문서 링크 참고.

## 1. 소프트 키보드 입력 깨짐 버그 수정

**증상**
- 어떤 언어든 입력 중 "1111111" 같은 문자가 원격 화면에 섞여 들어감
- 한글 입력 시 자음/모음 조합이 아예 전송 안 되는 경우 있음
- 키보드가 갑자기 닫혔다 다시 열리는 현상

**원인** (`flutter/lib/mobile/pages/remote_page.dart`)
안드로이드 소프트 키보드 입력 처리가 두 가지 경우만 가정하고 있었음:
- 텍스트가 늘어나면 "뒤에 순수하게 append됐다"고 가정 → `newValue.substring(oldValue.length)`로 잘라서 전송
- 텍스트가 줄어들면 backspace 1번

근데 실제 안드로이드 IME(자동완성, 예측 입력, 한글/CJK 조합)는 **텍스트 중간을 치환**하는 경우가 대부분:
- 길이가 같은 치환(자모가 합쳐져서 한 음절 블록 되는 경우) → 길이 비교 분기에서 아무 처리 안 하는 no-op 코드였음 → 조합된 한글이 통째로 씹힘
- 길이가 늘어나는 치환(자동완성) → 옛날 오프셋으로 자르다 보니 내부 anchor 패딩 문자열(`'1' * 1024`)의 일부가 그대로 새 나가서 "1111111"로 보임

**수정**
iOS 경로에서 이미 쓰던 방식과 동일하게, 공통 접두사(common prefix) 기준으로 실제 바뀐 부분만 계산해서 그 부분만 backspace + insert 하도록 변경. 클립보드 감지, 괄호 자동완성 특수 케이스는 그대로 유지.

- 커밋: `c5c4b7cb5` fix(android): correctly diff soft-keyboard IME edits instead of assuming pure append
- 실기기(Galaxy S25 Ultra → Windows 원격, 메모장)로 검증 완료

## 2. 원격 접속 중 백그라운드 연결 유지 (Client Keep-Alive)

원격 접속 중 화면 끄거나 다른 앱으로 전환하면 안드로이드가 앱을 죽여서 세션이 끊기는 문제 대응. Foreground Service + WakeLock + 배터리 최적화 예외 등록으로 백그라운드에서도 연결 유지하게 구현.

- 설계 문서: `docs/superpowers/specs/2026-09-05-android-client-background-keepalive-design.md`
- 구현 계획: `docs/superpowers/plans/2026-09-05-android-client-background-keepalive.md`
- 핵심 파일:
  - `flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/ClientKeepAliveService.kt` — foreground service 본체
  - `flutter/lib/common.dart` — `ClientKeepAliveManager` (refcounting, method channel 연동)
  - `flutter/lib/mobile/pages/remote_page.dart`, `view_camera_page.dart` — 원격/카메라 뷰 세션에서 keep-alive 연결
- 관련 커밋: `882b611b3`, `9d605e0d9`, `5912dc6f0`, `ea5122c7c`, `1dcb8edf7`, `df196959c`
- 동작은 always-on 자동 (사용자가 켜고 끄는 옵션 아님, [[user_profile]] 참고)
- 실기기로 원격 세션 유지 검증 완료

## 3. CI/빌드 설정

- `.github/workflows/flutter-ci.yml`, `flutter-build.yml`에 android-only `workflow_dispatch` 옵션 추가 — 포크에서 안드로이드만 빠르게 빌드/테스트하기 위함
- SBOM 생성 job이 쓰기 권한 없어서 실패하던 것 `contents: write` 권한 부여로 수정
- 커밋: `3642bea40`, `e5347eae9`

## 4. 알려진 이슈 — Galaxy에서 오탐(McAfee) 삭제/차단

Galaxy 기기의 Device Care(McAfee 엔진)가 이 포크 APK를 자동 삭제/차단하는 문제 있음. 구글 Play Protect와 공식 RustDesk 빌드는 안 걸림 — 서명 인증서 신뢰도 문제로 진단됨.

**현재 결정: 리스크 감수하고 계속 사용, 실제로 삭제/차단될 때만 McAfee 오탐 신고 진행.**

- 상세 원인 분석, 검토한 옵션, 신고 템플릿·링크: `docs/superpowers/plans/2026-09-05-android-galaxy-malware-false-positive.md`

## 현재 상태

- 브랜치: `android-soft-keyboard-fix`, working tree clean, 개인 포크에 push 완료
- 업스트림 PR 계획 없음 ([[feedback_no_upstream_prs_for_personal_changes]])
- 다음 세션에서 이어갈 것: 없음(대기 상태). Galaxy 오탐 재발 시 위 4번 문서부터 확인.

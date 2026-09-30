# Android 포크 작업 정리

날짜: 2026-09-05 작성, 2026-09-30 갱신 (5~8절 추가)
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

## 5. 소프트 키보드 입력 묶음(coalescing) — 추가 후 되돌림

**추가 (2026-09-07, `ef95bcfa8`)**
빠르게 한글을 입력하면 원격 PC에서 글자가 빠졌어요. 안드로이드 IME는 자모마다 `onChanged`를 한 번씩 호출해요. 앱은 호출마다 백스페이스와 텍스트를 보냈어요. 그래서 원격 PC의 입력 큐가 넘쳤어요. 이 커밋은 최신 텍스트만 보관하고 30ms마다 diff를 한 번만 보냈어요. 로컬 세션 기록은 secall의 `23b7ae19` 세션에 있어요.

**되돌림 (2026-09-29, `0f7318e83`, 원격 작업)**
30ms 루프는 시간만 기준으로 동작하고 IME 조합 상태를 몰라요. 또 툴바 키나 물리 키보드는 `inputModel`로 바로 보내요. 그래서 묶음 대기 중인 텍스트보다 이 입력이 먼저 갈 수 있고, 입력 순서가 바뀌어요. 이 커밋은 1절의 공통 접두사 diff(`c5c4b7cb5`)로 돌아갔어요. 글자 누락 문제는 6절이 다른 방법으로 다뤄요.

## 6. 조합 중인 한글/CJK 글자를 diff에서 빼기

- 커밋: `8830388df` (2026-09-29, 원격 작업)
- 파일: `flutter/lib/mobile/pages/remote_page.dart`

**문제**
안드로이드 경로는 조합의 각 단계(자모 → 음절)를 백스페이스와 텍스트로 보냈어요. 백스페이스 수는 로컬의 `_value` 버퍼만 보고 계산했어요. 그래서 중간 단계 하나가 빠지거나 순서가 바뀌면 다음 백스페이스가 앞 글자를 지웠어요. 또는 옛 조합 단계가 원격에 남았어요.

**수정**
- `_withoutNonAsciiComposing()`은 텍스트에서 아직 조합 중인 구간을 빼요. 이 구간에 ASCII가 아닌 글자가 있을 때만 빼요. 그래서 원격에는 조합을 끝낸 글자만 가요.
- ASCII 조합(영어 예측 입력 등)은 전과 같이 바로 보내요.
- `_flushCommittedComposition()`은 `_textController`의 리스너예요. 조합이 끝나도 텍스트가 바뀌지 않으면 `onChanged`가 호출되지 않아요. 이 리스너가 그 경우에 diff를 보내요. iOS에서는 등록하지 않아요.
- 실기기(Galaxy S25 Ultra)에서 사용자가 동작을 확인했어요(2026-09-30).

## 7. 세 손가락 스크롤 속도 옵션

- 커밋: `0f180bb18` (2026-09-30, 원격 작업)
- 파일: `flutter/lib/common/widgets/remote_input.dart`, `flutter/lib/mobile/pages/settings_page.dart`, `flutter/lib/consts.dart`, `src/lang/*.rs`

**문제**
세 손가락 세로 드래그는 약 4px마다 휠 한 칸을 보냈어요. 속도를 바꾸려면 원격 PC의 시스템 휠 설정을 바꿔야 했어요. 그 설정은 원격 PC의 실제 마우스에도 영향을 줘요.

**수정**
- 로컬 옵션 `three-finger-scroll-speed`(`kOptionThreeFingerScrollSpeed`)를 추가했어요. 범위는 0.1x~10x이고 기본값은 1x예요.
- 설정 위치는 Display Settings의 "Three-finger scroll speed" 항목이에요. 누르면 숫자 입력 창이 열려요. 범위 밖의 값은 저장하지 않아요.
- `threeFingerScrollSpeed()`가 옵션 값을 읽어요. 값이 없거나 0 이하면 1.0을 쓰고, 10보다 크면 10을 써요.
- 드래그 누적값에 속도를 곱해요. 1x보다 크면 누적된 칸을 모두 한 번에 보내요. 터치 업데이트마다 한 칸만 보내면 속도에 상한이 생기기 때문이에요.
- 기본값 1x에서는 전과 같이 동작해요.
- 원격 기기가 안드로이드이면 앱은 이 제스처를 처리하지 않아요(`isPeerAndroid`). 그래서 이 옵션도 적용되지 않아요.
- 번역 키 "Three-finger scroll speed"를 `template.rs`와 모든 언어 파일에 추가했어요. `ko.rs`의 값은 "세 손가락 스크롤 속도"예요.
- 실기기(Galaxy S25 Ultra)에서 사용자가 동작을 확인했어요(2026-09-30).

## 8. 로컬 Windows 빌드 환경

- 커밋: `9dd586a06`, `a23038a37`, `79256c481`, `6d4ad1958`, `62fd13e1a` (2026-09-07), `8f40fdbc1` (2026-09-08), `90015ed24` (2026-09-30)
- 사용법: `build-local/README.md`

CI는 Ubuntu에서 약 45분 걸려요. Dart만 고친 경우에도 같아요. 그래서 Windows에서 APK를 빌드하는 `build-local/build.py`를 만들었어요. 모든 도구는 E: 빌드 파티션에서 가져오고, 필요한 버전은 `build-local/use_tool.json`에 적어요. 서명은 개인 키(`rustdesk-personal`)로 해요. 로컬 세션 기록은 secall의 `f2845f63` 세션에 있어요.

`scripts/build_and_install.py`는 빌드가 끝나면 연결된 adb 장치를 보여줘요. 사용자가 고른 장치에 `adb install -r`로 설치해요. 그래서 앱 데이터가 남아요. 과정은 [세션 기록](sessions/2026-09-30-remote-pull-build-install.md)에 있어요.

## 현재 상태

- 브랜치: `master` (2026-09-30에 `android-soft-keyboard-fix`를 master로 fast-forward 머지하고 브랜치를 지웠어요). 개인 포크에 push 완료
- 앞으로 작업은 로컬에서 해요. 원격 작업은 2026-09-29~30의 한 번이었어요.
- 업스트림 PR 계획 없음 ([[feedback_no_upstream_prs_for_personal_changes]])
- 다음 세션에서 이어갈 것: 없음(대기 상태). Galaxy 오탐 재발 시 위 4번 문서부터 확인.

## 세션 기록

- [2026-09-30 원격 작업 가져오기, 빌드, 설치 스크립트, master 정리](sessions/2026-09-30-remote-pull-build-install.md)

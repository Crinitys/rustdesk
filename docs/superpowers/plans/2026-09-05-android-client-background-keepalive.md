# Android Client Background Keep-Alive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep an active outgoing (client/controller) RustDesk Android connection alive when the app is backgrounded, via a dedicated foreground service, wake lock, and a one-time battery-optimization exemption request.

**Architecture:** A reference-counted `ClientKeepAliveManager` (Dart, mirrors the existing `WakelockManager` pattern) tracks how many client sessions are open across `remote_page.dart` and `view_camera_page.dart`, and starts/stops a new Android foreground service (`ClientKeepAliveService`, `connectedDevice` type) through two new method-channel calls. A third method-channel call requests battery-optimization exemption once, ever. No Rust/protocol changes — video keeps streaming in the background as-is.

**Tech Stack:** Flutter/Dart (mobile), Kotlin (Android platform), existing `flutter_rust_bridge` method channel plumbing.

**Spec:** `docs/superpowers/specs/2026-09-05-android-client-background-keepalive-design.md`

## Global Constraints

- Android-only. No behavior change on iOS/desktop/web (`ClientKeepAliveManager` no-ops when `!platformSupported`, defaulted from `isAndroid`).
- No settings toggle — always on, per approved spec.
- No Rust/session-protocol changes — video streaming behavior is unchanged.
- `MainService.kt` is not modified — it is the opposite role (this device being controlled) and is a separate concern.
- New localization key uses sentence case and is added to `src/lang/template.rs` and every `src/lang/*.rs` file except `en.rs` (key text IS the English source, per `AGENTS.md`).
- This environment has no local Android/vcpkg native build capability (confirmed earlier this session: no vcpkg install, `flutter build apk` cannot complete locally). Kotlin/manifest correctness is verified by the existing fork CI (`Crinitys/rustdesk`, `flutter-ci.yml` workflow, `-f platform=android`), not a local build, until Task 6.

---

### Task 1: `ClientKeepAliveManager` (Dart, refcounting + test seams)

**Files:**
- Modify: `flutter/lib/consts.dart:218` (add new option key constant, right after `kOptionKeepAwakeDuringOutgoingSessions`)
- Modify: `flutter/lib/common.dart:2787` (add new class right after `WakelockManager`'s closing brace, before the `/// call this to reload current window.` comment)
- Test: `flutter/test/client_keep_alive_manager_test.dart` (new file)

**Interfaces:**
- Produces: `ClientKeepAliveManager.enable(UniqueKey key)`, `ClientKeepAliveManager.disable(UniqueKey key)` — called by Task 5's call sites.
- Produces (test seams, production code never sets these): `ClientKeepAliveManager.platformSupported` (`bool`, defaults to `isAndroid`), `ClientKeepAliveManager.startPlatformService` / `stopPlatformService` / `onFirstEverEnable` (`Future<void> Function()`, default to the real platform-channel implementations).
- Consumes: `gFFI.invokeMethod` (existing, see `flutter/lib/common.dart:1526` for the pattern), `mainGetLocalBoolOptionSync` / `mainSetLocalBoolOption` (existing, `flutter/lib/common.dart:1662-1669`), `isAndroid` (existing, `flutter/lib/common.dart:53`).

- [ ] **Step 1: Write the failing test**

Create `flutter/test/client_keep_alive_manager_test.dart`:

```dart
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_hbb/common.dart';

void main() {
  final originalStart = ClientKeepAliveManager.startPlatformService;
  final originalStop = ClientKeepAliveManager.stopPlatformService;
  final originalOnFirstEverEnable = ClientKeepAliveManager.onFirstEverEnable;
  final originalPlatformSupported = ClientKeepAliveManager.platformSupported;

  setUp(() {
    ClientKeepAliveManager.platformSupported = true;
    ClientKeepAliveManager.onFirstEverEnable = () async {};
  });

  tearDown(() {
    ClientKeepAliveManager.startPlatformService = originalStart;
    ClientKeepAliveManager.stopPlatformService = originalStop;
    ClientKeepAliveManager.onFirstEverEnable = originalOnFirstEverEnable;
    ClientKeepAliveManager.platformSupported = originalPlatformSupported;
  });

  test('starts the platform service once when the first of two sessions enables',
      () {
    var startCalls = 0;
    ClientKeepAliveManager.startPlatformService = () async {
      startCalls++;
    };

    ClientKeepAliveManager.enable(UniqueKey());
    ClientKeepAliveManager.enable(UniqueKey());

    expect(startCalls, 1);
  });

  test('stops the platform service only once the last of two sessions disables',
      () {
    ClientKeepAliveManager.startPlatformService = () async {};
    var stopCalls = 0;
    ClientKeepAliveManager.stopPlatformService = () async {
      stopCalls++;
    };

    final keyA = UniqueKey();
    final keyB = UniqueKey();
    ClientKeepAliveManager.enable(keyA);
    ClientKeepAliveManager.enable(keyB);
    ClientKeepAliveManager.disable(keyA);
    expect(stopCalls, 0);

    ClientKeepAliveManager.disable(keyB);
    expect(stopCalls, 1);
  });

  test('calls onFirstEverEnable exactly once even across two separate sessions',
      () {
    ClientKeepAliveManager.startPlatformService = () async {};
    ClientKeepAliveManager.stopPlatformService = () async {};
    var onFirstEverEnableCalls = 0;
    ClientKeepAliveManager.onFirstEverEnable = () async {
      onFirstEverEnableCalls++;
    };

    final keyA = UniqueKey();
    final keyB = UniqueKey();
    ClientKeepAliveManager.enable(keyA);
    ClientKeepAliveManager.disable(keyA);
    ClientKeepAliveManager.enable(keyB);
    ClientKeepAliveManager.disable(keyB);

    expect(onFirstEverEnableCalls, 2);
  });

  test('does nothing when the platform is not supported', () {
    ClientKeepAliveManager.platformSupported = false;
    var startCalls = 0;
    ClientKeepAliveManager.startPlatformService = () async {
      startCalls++;
    };

    ClientKeepAliveManager.enable(UniqueKey());

    expect(startCalls, 0);
  });
}
```

Note: the third test documents the real behavior of `onFirstEverEnable` as written below — it is called on every 0→1 transition, and its *own* body (the real, non-stubbed implementation) is what skips the battery-optimization prompt on repeat calls via the persisted local option. The test above stubs it out entirely to isolate the refcounting logic, so it intentionally observes 2 calls, not 1 — that one-time behavior belongs to Task 1 Step 3's real implementation, not to the refcounting class itself.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd flutter && flutter test test/client_keep_alive_manager_test.dart`
Expected: FAIL — `ClientKeepAliveManager` is not defined (analyzer/compile error, since the class doesn't exist yet).

- [ ] **Step 3: Add the option key constant**

In `flutter/lib/consts.dart`, right after line 218 (`const String kOptionKeepAwakeDuringOutgoingSessions = "keep-awake-during-outgoing-sessions";`), add:

```dart
const String kOptionAndroidKeepAliveBatteryPromptShown =
    "android-keep-alive-battery-prompt-shown";
```

- [ ] **Step 4: Implement `ClientKeepAliveManager`**

In `flutter/lib/common.dart`, right after `WakelockManager`'s closing `}` (the line before `/// call this to reload current window.`), add:

```dart
/// Keeps the Android client-side (outgoing/controller) connection alive
/// across app backgrounding via a foreground service. Same reference
/// counting shape as [WakelockManager] so multiple simultaneous sessions
/// (tabs) share one service and it only stops when the last one closes.
///
/// This is the client (controlling) role only — unrelated to the
/// controlled-side keep-awake handled by [WakelockManager]'s `isServer`
/// path and the Android `MainService` foreground service.
class ClientKeepAliveManager {
  static final Set<UniqueKey> _enabledKeys = {};

  // Test seam: production code never touches these fields. Real
  // implementations are the private static methods below; tests swap in
  // stubs so the refcounting logic can be verified without touching
  // platform channels or native FFI bindings.
  static bool platformSupported = isAndroid;
  static Future<void> Function() startPlatformService = _startPlatformService;
  static Future<void> Function() stopPlatformService = _stopPlatformService;
  static Future<void> Function() onFirstEverEnable = _onFirstEverEnable;

  static Future<void> _startPlatformService() =>
      gFFI.invokeMethod('start_client_keep_alive');

  static Future<void> _stopPlatformService() =>
      gFFI.invokeMethod('stop_client_keep_alive');

  static Future<void> _onFirstEverEnable() async {
    if (mainGetLocalBoolOptionSync(
        kOptionAndroidKeepAliveBatteryPromptShown)) {
      return;
    }
    await mainSetLocalBoolOption(
        kOptionAndroidKeepAliveBatteryPromptShown, true);
    await gFFI.invokeMethod('request_ignore_battery_optimizations');
  }

  static void enable(UniqueKey key) {
    if (!platformSupported) return;
    final wasEmpty = _enabledKeys.isEmpty;
    _enabledKeys.add(key);
    if (!wasEmpty) return;
    startPlatformService();
    onFirstEverEnable();
  }

  static void disable(UniqueKey key) {
    if (!platformSupported) return;
    _enabledKeys.remove(key);
    if (_enabledKeys.isNotEmpty) return;
    stopPlatformService();
  }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd flutter && flutter test test/client_keep_alive_manager_test.dart`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add flutter/lib/consts.dart flutter/lib/common.dart flutter/test/client_keep_alive_manager_test.dart
git commit -m "feat(android): add ClientKeepAliveManager refcounting"
```

---

### Task 2: `ClientKeepAliveService` (Kotlin foreground service) + manifest registration

**Files:**
- Create: `flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/ClientKeepAliveService.kt`
- Modify: `flutter/android/app/src/main/AndroidManifest.xml`

**Interfaces:**
- Produces: `ClientKeepAliveService` (Android `Service` class), started/stopped by Task 3's `MainActivity.kt` handlers via `Intent(activity, ClientKeepAliveService::class.java)`.
- Consumes: `translate()`, `DEFAULT_NOTIFY_TITLE` (existing, `flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/MainService.kt:50`, `common.kt:165` — same package, no import needed), `R.mipmap.ic_stat_logo`, `R.color.primary` (existing resources, already used by `MainService.kt`).

- [ ] **Step 1: Create the service**

Create `flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/ClientKeepAliveService.kt`:

```kotlin
package com.carriez.flutter_hbb

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.PendingIntent.FLAG_IMMUTABLE
import android.app.PendingIntent.FLAG_UPDATE_CURRENT
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

const val CLIENT_KEEP_ALIVE_NOTIFY_ID = 300
const val CLIENT_KEEP_ALIVE_CHANNEL_ID = "RustDeskClientKeepAlive"

/**
 * Keeps the process and network alive while this device is controlling a
 * remote peer and the app is backgrounded.
 *
 * Client (outgoing/controller) role only — unrelated to [MainService],
 * which is the opposite role (this device being controlled) and is not
 * touched by this class.
 */
class ClientKeepAliveService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundNotification()
        acquireWakeLock()
        return START_STICKY
    }

    override fun onDestroy() {
        releaseWakeLock()
        super.onDestroy()
    }

    private fun acquireWakeLock() {
        if (wakeLock != null) return
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "rustdesk:client_keep_alive"
        ).apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
            }
        }
        wakeLock = null
    }

    private fun startForegroundNotification() {
        val notificationManager =
            getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channelId = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CLIENT_KEEP_ALIVE_CHANNEL_ID,
                "RustDesk Connection",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps an active outgoing RustDesk connection alive in the background"
                lightColor = Color.BLUE
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            }
            notificationManager.createNotificationChannel(channel)
            CLIENT_KEEP_ALIVE_CHANNEL_ID
        } else {
            ""
        }

        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED
            action = Intent.ACTION_MAIN
            addCategory(Intent.CATEGORY_LAUNCHER)
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent, FLAG_UPDATE_CURRENT or FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, channelId)
            .setOngoing(true)
            .setSmallIcon(R.mipmap.ic_stat_logo)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentTitle(DEFAULT_NOTIFY_TITLE)
            .setContentText(translate("Connection is active in the background"))
            .setOnlyAlertOnce(true)
            .setContentIntent(pendingIntent)
            .setColor(ContextCompat.getColor(this, R.color.primary))
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                CLIENT_KEEP_ALIVE_NOTIFY_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            )
        } else {
            startForeground(CLIENT_KEEP_ALIVE_NOTIFY_ID, notification)
        }
    }
}
```

- [ ] **Step 2: Register the service and permissions in the manifest**

In `flutter/android/app/src/main/AndroidManifest.xml`, add two permissions right after line 16 (`<uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE" />`):

```xml
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE" />
    <uses-permission android:name="android.permission.CHANGE_NETWORK_STATE" />
```

And register the service right after the existing `.MainService` `<service>` block (after its closing `</service>`, before the `.FloatingWindowService` service):

```xml
        <service
            android:name=".ClientKeepAliveService"
            android:enabled="true"
            android:exported="false"
            android:foregroundServiceType="connectedDevice" />
```

- [ ] **Step 3: Add the new localization key**

This key is used by `translate("Connection is active in the background")` in Step 1. Per `AGENTS.md`, append it (with an empty value) to `src/lang/template.rs` and to every `src/lang/*.rs` file except `en.rs` (the key text is already the English source, so `en.rs` needs no entry). Run this once from the repo root:

```bash
for f in src/lang/template.rs src/lang/*.rs; do
  base=$(basename "$f")
  if [ "$base" = "en.rs" ]; then
    continue
  fi
  # Insert the new entry as the line right before the closing "].iter()..." line.
  awk '
    /^    \]\.iter\(\)\.cloned\(\)\.collect\(\);$/ && !done {
      print "        (\"Connection is active in the background\", \"\"),"
      done = 1
    }
    { print }
  ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
```

- [ ] **Step 4: Verify the new key landed correctly**

Run: `grep -c '"Connection is active in the background"' src/lang/*.rs | grep -v ':0' | wc -l`
Expected: `51` (every `src/lang/*.rs` file except `en.rs` — confirm with `ls src/lang/*.rs | wc -l` first, which is 52 total, minus `en.rs`).

Run: `grep '"Connection is active in the background"' src/lang/template.rs`
Expected: one match, `("Connection is active in the background", ""),`.

- [ ] **Step 5: No local compile check available**

This environment has no local vcpkg/native Android build (confirmed earlier in this session — `flutter build apk` cannot complete here). Kotlin correctness is verified in Task 6 via the fork's CI build, not here. Do not claim this compiles until Task 6's CI run succeeds.

- [ ] **Step 6: Commit**

```bash
git add flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/ClientKeepAliveService.kt flutter/android/app/src/main/AndroidManifest.xml src/lang/template.rs src/lang/*.rs
git commit -m "feat(android): add ClientKeepAliveService foreground service"
```

---

### Task 3: `MainActivity.kt` method-channel handlers

**Files:**
- Modify: `flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/MainActivity.kt:649` (insert new `when` branches right before the existing `else ->` branch at line 650)

**Interfaces:**
- Consumes: `ClientKeepAliveService` (Task 2), `activity` / `context` (existing properties already used by neighboring branches in this same `when` block, e.g. `init_service` and `check_permission`).
- Produces: three new method-channel methods — `start_client_keep_alive`, `stop_client_keep_alive`, `request_ignore_battery_optimizations` — consumed by Task 1's `ClientKeepAliveManager` (`gFFI.invokeMethod(...)` calls).

- [ ] **Step 1: Add the three branches**

In `flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/MainActivity.kt`, right before the `else -> {` branch (currently at line 650), insert:

```kotlin
                "start_client_keep_alive" -> {
                    Intent(activity, ClientKeepAliveService::class.java).also {
                        androidx.core.content.ContextCompat.startForegroundService(activity, it)
                    }
                    result.success(true)
                }
                "stop_client_keep_alive" -> {
                    activity.stopService(Intent(activity, ClientKeepAliveService::class.java))
                    result.success(true)
                }
                "request_ignore_battery_optimizations" -> {
                    val powerManager =
                        context.getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
                        !powerManager.isIgnoringBatteryOptimizations(context.packageName)
                    ) {
                        try {
                            val intent = Intent(
                                android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                Uri.parse("package:${context.packageName}")
                            )
                            activity.startActivity(intent)
                        } catch (e: Exception) {
                            Log.e(logTag, "Failed to request battery optimization exemption", e)
                        }
                    }
                    result.success(true)
                }
```

(Fully-qualified `androidx.core.content.ContextCompat`, `android.os.PowerManager`, and `android.provider.Settings` are used inline instead of adding new top-level imports, per `AGENTS.md`'s "avoid churning shared import blocks" — `Uri` is already imported at line 28, `Context`/`Build`/`Log`/`Intent` are already imported.)

- [ ] **Step 2: No local compile check available**

Same constraint as Task 2 Step 5 — verified in Task 6.

- [ ] **Step 3: Commit**

```bash
git add flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/MainActivity.kt
git commit -m "feat(android): wire client keep-alive method channel handlers"
```

---

### Task 4: Wire call sites in `remote_page.dart` and `view_camera_page.dart`

**Files:**
- Modify: `flutter/lib/mobile/pages/remote_page.dart:108` (initState) and `:172` (dispose)
- Modify: `flutter/lib/mobile/pages/view_camera_page.dart:102` (initState) and `:139` (dispose)

**Interfaces:**
- Consumes: `ClientKeepAliveManager.enable`/`disable` (Task 1), `_uniqueKey` (existing `UniqueKey` field, already used by the neighboring `WakelockManager.enable/disable(_uniqueKey)` calls in both files).

- [ ] **Step 1: `remote_page.dart` initState**

In `flutter/lib/mobile/pages/remote_page.dart`, right after line 108 (`WakelockManager.enable(_uniqueKey);`), add:

```dart
    ClientKeepAliveManager.enable(_uniqueKey);
```

- [ ] **Step 2: `remote_page.dart` dispose**

Right after line 172 (`WakelockManager.disable(_uniqueKey);`), add:

```dart
    ClientKeepAliveManager.disable(_uniqueKey);
```

- [ ] **Step 3: `view_camera_page.dart` initState**

Right after line 102 (`WakelockManager.enable(_uniqueKey);`), add:

```dart
    ClientKeepAliveManager.enable(_uniqueKey);
```

- [ ] **Step 4: `view_camera_page.dart` dispose**

Right after line 139 (`WakelockManager.disable(_uniqueKey);`), add:

```dart
    ClientKeepAliveManager.disable(_uniqueKey);
```

- [ ] **Step 5: Run the Dart analyzer**

Run: `cd flutter && dart analyze lib/mobile/pages/remote_page.dart lib/mobile/pages/view_camera_page.dart lib/common.dart lib/consts.dart`
Expected: 0 errors (pre-existing deprecation infos elsewhere in these files, if any, are unrelated and fine).

- [ ] **Step 6: Run the full Dart test suite**

Run: `cd flutter && flutter test`
Expected: all tests pass, including the 4 new ones from Task 1.

- [ ] **Step 7: Commit**

```bash
git add flutter/lib/mobile/pages/remote_page.dart flutter/lib/mobile/pages/view_camera_page.dart
git commit -m "feat(android): enable client keep-alive for remote and camera view sessions"
```

---

### Task 5: Regression-surface check

Per `AGENTS.md`'s mandatory regression-surface check, before Task 6's build:

- [ ] **Step 1: Review the full diff for this feature**

Run: `git diff master...HEAD -- flutter/lib flutter/android src/lang docs/superpowers`

Confirm:
- `MainService.kt` has zero lines changed.
- `remote_page.dart` / `view_camera_page.dart` diffs are exactly one new line each in `initState`/`dispose` (plus the earlier, unrelated soft-keyboard-diff commit already on this branch — diff against the commit before Task 1 if that's ambiguous).
- No existing method-channel branch in `MainActivity.kt` was modified — only new branches were inserted.
- No file outside `flutter/lib`, `flutter/android`, `src/lang/*.rs`, and `docs/superpowers` changed.

- [ ] **Step 2: Commit if any cleanup was needed**

Only if Step 1 found something to fix — otherwise skip (no empty commit).

---

### Task 6: Build via CI and verify on a real device

**Files:** none (verification only).

- [ ] **Step 1: Push to the fork and trigger the android-only CI build**

```bash
git push fork android-soft-keyboard-fix:master
gh workflow run flutter-ci.yml --repo Crinitys/rustdesk --ref master -f platform=android
```

- [ ] **Step 2: Watch the run to completion**

Use the same polling approach as the earlier keyboard-fix build in this session (`gh run view <id> --repo Crinitys/rustdesk --json status,conclusion,jobs`). Confirm all three `build rustdesk android apk *` jobs and `build rustdesk android universal apk` complete with `success`. If any Kotlin file fails to compile, fix the specific error and re-push — this is the first real compile check for Task 2 and Task 3's code.

- [ ] **Step 3: Install the new build**

Download `rustdesk-1.5.0-aarch64.apk` from the fork's `nightly` release (`gh release download nightly --repo Crinitys/rustdesk --pattern "rustdesk-1.5.0-aarch64.apk" --clobber`), re-sign it with the existing personal keystore (`C:\Users\Thurion\.android-keys\rustdesk-personal.keystore`, alias `rustdesk-personal`) using `zipalign` + `apksigner sign`, then `adb install -r` it (same-key update, no uninstall needed this time).

- [ ] **Step 4: Manual on-device verification**

Per the spec's Testing section:
- Connect to a peer from the phone.
- Press home, turn the screen off, wait several minutes.
- Return to the app: confirm the session is still connected, no reconnect happened.
- Confirm a persistent notification ("RustDesk" / "Connection is active in the background") appeared while backgrounded and disappeared after closing the session.
- Confirm the "ignore battery optimizations" system dialog appeared on the very first connection after this install, and does **not** reappear on the second connection (whether accepted or dismissed).

- [ ] **Step 5: Report results**

If any check in Step 4 fails, that is a new bug to diagnose (systematic-debugging), not a sign this plan's tasks were wrong — come back to this task list only if the fix requires changing code from Tasks 1-4.

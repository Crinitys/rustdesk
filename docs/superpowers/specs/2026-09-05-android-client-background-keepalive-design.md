# Android client background keep-alive

## Problem

On Android, when the user backgrounds the app (home button) while controlling
a remote peer, the OS can suspend the process and/or restrict network access
after some time (more aggressively on OEMs like Samsung One UI). This causes
the active client-side connection to drop silently, or to be mid-reconnect
by the time the user returns, at which point the peer has already closed the
session. There is currently no mechanism in this codebase to keep an
*outgoing* (client/controller) connection alive across backgrounding — the
existing `MainService.kt` foreground service is for the opposite role (this
device being controlled, i.e. screen capture), and is not reused for this.

## Goal

Keep an active outgoing RustDesk connection alive across backgrounding,
automatically, with no user-facing setting. Video keeps streaming while
backgrounded (user's explicit choice — simplicity over battery/data savings).

## Non-goals

- No settings toggle to disable this (always-on, per user decision).
- No pause/resume of the video stream — out of scope, no Rust/session
  protocol changes needed.
- No change to `MainService.kt` or the controlled-side (incoming session)
  keep-awake behavior — unrelated concern, left untouched.
- iOS/desktop: not applicable (no equivalent OS background-kill behavior /
  no existing precedent), guarded off.

## Design

Mirrors the existing `WakelockManager` pattern already used for the same
two call sites (`remote_page.dart`, `view_camera_page.dart`): a reference
counted enable(key)/disable(key) manager, so multiple simultaneous client
sessions (tabs) share one service/notification and it only stops when the
last one closes.

### New components

1. **`flutter/android/app/src/main/kotlin/com/carriez/flutter_hbb/ClientKeepAliveService.kt`** (new file)
   - Minimal foreground `Service`. On start: `startForeground()` with a
     fixed low-priority notification, `foregroundServiceType="connectedDevice"`
     (network control of an external device — matches Android's own
     guidance for this case, and avoids the 6-hour cap that `dataSync`
     carries on Android 15+). Acquires a `PowerManager` `PARTIAL_WAKE_LOCK`
     so CPU stays responsive for continuous video decode while the screen
     is off. On stop: releases the wake lock, calls `stopForeground`.
   - Does not touch `MainService.kt`.

2. **AndroidManifest.xml**
   - Register the new `<service>` with
     `android:foregroundServiceType="connectedDevice"`.
   - Add `FOREGROUND_SERVICE_CONNECTED_DEVICE` and `CHANGE_NETWORK_STATE`
     permissions (required by Android 14+ for the `connectedDevice` type).
     `FOREGROUND_SERVICE`, `WAKE_LOCK`, and
     `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` already exist and are reused.

3. **`MainActivity.kt`**
   - Add method-channel handlers: `start_client_keep_alive`,
     `stop_client_keep_alive`, `request_ignore_battery_optimizations`.
     New branches in the existing routing, no restructuring.

4. **`flutter/lib/common.dart`**
   - New `ClientKeepAliveManager` class, placed beside `WakelockManager`,
     same enable(key)/disable(key) reference-counting shape. Android-only
     (no-op elsewhere). On the 0→1 transition: starts the service, and if
     this is the first time ever (checked via a persisted local option,
     same mechanism the codebase already uses for other one-time flags)
     and the app isn't already exempted, fires the battery-optimization
     exemption request. Declining is not re-prompted on later connections.
     On the 1→0 transition: stops the service.

5. **Call sites**: `remote_page.dart` and `view_camera_page.dart`, same
   `initState`/`dispose` lines that already call
   `WakelockManager.enable(_uniqueKey)` / `disable(_uniqueKey)` — one
   additional line each for `ClientKeepAliveManager`.

### Error handling

If the user declines the battery-optimization exemption dialog, the feature
degrades gracefully: foreground service + wake lock still apply (helps on
most devices), just not guaranteed bulletproof on the most aggressive OEM
battery managers. No retry-nagging on every connection.

### Testing

Manual, on a real device (no emulator substitute for OEM background-kill
behavior): connect to a peer, background the app (home button, screen off)
for several minutes, confirm the session is still connected on return with
no reconnect needed. Confirm the notification appears/disappears with
session count, and the battery-optimization dialog appears once (first
connection ever) and does not repeat after being dismissed either way.

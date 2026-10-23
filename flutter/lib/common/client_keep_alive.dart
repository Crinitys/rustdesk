import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_hbb/common.dart';

const String kOptionAndroidKeepAliveBatteryPromptShown =
    "android-keep-alive-battery-prompt-shown";

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

  /// Test seam: resets internal state for test isolation between test cases.
  /// Production code never calls this.
  @visibleForTesting
  static void resetForTest() => _enabledKeys.clear();
}

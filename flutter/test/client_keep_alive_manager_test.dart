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

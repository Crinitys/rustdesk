import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/models/input_model.dart';
import 'package:flutter_hbb/models/platform_model.dart';

typedef KeyHelpButton = Widget Function(String text, void Function() onPressed,
    {bool? active, IconData? icon});

final Map<SessionID, ValueNotifier<bool>> _enabled = {};

List<Widget> vmKeyboardButtons(
    SessionID sessionId, InputModel inputModel, KeyHelpButton wrap) {
  final notifier = _enabled.putIfAbsent(sessionId, () {
    final notifier = ValueNotifier<bool>(false);
    bind
        .sessionGetPeerOption(sessionId: sessionId, name: 'vm-keyboard-input')
        .then((value) => notifier.value = value == 'Y');
    return notifier;
  });
  return [
    ValueListenableBuilder<bool>(
      valueListenable: notifier,
      builder: (context, enabled, _) => wrap('VM', () {
        final v = !notifier.value;
        notifier.value = v;
        bind.sessionPeerOption(
            sessionId: sessionId,
            name: 'vm-keyboard-input',
            value: v ? 'Y' : '');
      }, active: enabled),
    ),
    wrap('한/영', () {
      inputModel.inputKey('VM_HANGUL_TOGGLE');
    }),
  ];
}

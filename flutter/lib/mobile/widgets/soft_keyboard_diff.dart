import 'package:flutter/services.dart';

class SoftKeyboardEdit {
  final int removed;
  final String content;
  const SoftKeyboardEdit(this.removed, this.content);
}

SoftKeyboardEdit softKeyboardDiff(String oldValue, String newValue) {
  var common = 0;
  final maxCommon =
      oldValue.length < newValue.length ? oldValue.length : newValue.length;
  while (common < maxCommon && oldValue[common] == newValue[common]) {
    common++;
  }
  return SoftKeyboardEdit(
      oldValue.length - common, newValue.substring(common));
}

// Hold a still-composing Hangul/CJK syllable out of the diff, so jamo stages
// are not sent as backspace + text; it is sent once the IME commits it.
String withoutNonAsciiComposing(TextEditingValue value, String text) {
  final composing = value.composing;
  if (!composing.isValid ||
      composing.isCollapsed ||
      composing.end > text.length) {
    return text;
  }
  final composed = text.substring(composing.start, composing.end);
  if (composed.runes.every((r) => r < 0x80)) {
    return text;
  }
  return text.substring(0, composing.start) + text.substring(composing.end);
}

class CompositionCommitWatcher {
  bool _wasComposing = false;

  // Committing a composition may not change the text, so `onChanged` is not
  // called for it.
  bool ended(TextEditingValue value) {
    final composing = value.composing;
    final isComposing = composing.isValid && !composing.isCollapsed;
    final ended = _wasComposing && !isComposing;
    _wasComposing = isComposing;
    return ended;
  }
}

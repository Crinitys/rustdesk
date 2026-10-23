import 'package:flutter/services.dart';
import 'package:flutter_hbb/mobile/widgets/soft_keyboard_diff.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('softKeyboardDiff', () {
    test('append', () {
      final edit = softKeyboardDiff('ab', 'abc');
      expect(edit.removed, 0);
      expect(edit.content, 'c');
    });

    test('backspace', () {
      final edit = softKeyboardDiff('abc', 'ab');
      expect(edit.removed, 1);
      expect(edit.content, '');
    });

    test('Hangul replace, same length', () {
      final edit = softKeyboardDiff('가', '각');
      expect(edit.removed, 1);
      expect(edit.content, '각');
    });

    test('autocorrect', () {
      final edit = softKeyboardDiff('teh', 'the ');
      expect(edit.removed, 2);
      expect(edit.content, 'he ');
    });

    test('no change', () {
      final edit = softKeyboardDiff('ab', 'ab');
      expect(edit.removed, 0);
      expect(edit.content, '');
    });
  });

  group('withoutNonAsciiComposing', () {
    test('drops a composing Hangul syllable', () {
      final value = TextEditingValue(
        text: 'a한',
        selection: const TextSelection.collapsed(offset: 2),
        composing: const TextRange(start: 1, end: 2),
      );
      expect(withoutNonAsciiComposing(value, 'a한'), 'a');
    });

    test('keeps a composing ASCII range', () {
      final value = TextEditingValue(
        text: 'ab',
        selection: const TextSelection.collapsed(offset: 2),
        composing: const TextRange(start: 0, end: 2),
      );
      expect(withoutNonAsciiComposing(value, 'ab'), 'ab');
    });

    test('keeps text when the composing range is invalid', () {
      const value = TextEditingValue(text: 'a한');
      expect(withoutNonAsciiComposing(value, 'a한'), 'a한');
    });
  });

}

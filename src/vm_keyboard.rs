pub const OPTION_VM_KEYBOARD: &str = "vm-keyboard-input";
pub const HANGUL_TOGGLE_KEY_NAME: &str = "VM_HANGUL_TOGGLE";
pub const HANGUL_TOGGLE_SCANCODE: u32 = 0x72;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ScanEvent {
    pub scancode: u32,
    pub down: bool,
}

#[derive(Debug, Default)]
pub struct VmKeyboard {
    hangul: bool,
    composing: bool,
}

pub static CLIENT: std::sync::Mutex<VmKeyboard> = std::sync::Mutex::new(VmKeyboard {
    hangul: false,
    composing: false,
});

const LETTER_SCANCODES: [u32; 26] = [
    0x1E, 0x30, 0x2E, 0x20, 0x12, 0x21, 0x22, 0x23, 0x17, 0x24, 0x25, 0x26, 0x32, 0x31, 0x18, 0x19,
    0x10, 0x13, 0x1F, 0x14, 0x16, 0x2F, 0x11, 0x2D, 0x15, 0x2C,
];
const INITIALS: [&str; 19] = [
    "r", "R", "s", "e", "E", "f", "a", "q", "Q", "t", "T", "d", "w", "W", "c", "z", "x", "v", "g",
];
const VOWELS: [&str; 21] = [
    "k", "o", "i", "O", "j", "p", "u", "P", "h", "hk", "ho", "hl", "y", "n", "nj", "np", "nl", "b",
    "m", "ml", "l",
];
const FINALS: [&str; 28] = [
    "", "r", "R", "rt", "s", "sw", "sg", "e", "f", "fr", "fa", "fq", "ft", "fx", "fv", "fg", "a",
    "q", "qt", "t", "T", "d", "w", "c", "z", "x", "v", "g",
];
const JAMO: [&str; 51] = [
    "r", "R", "rt", "s", "sw", "sg", "e", "E", "f", "fr", "fa", "fq", "ft", "fx", "fv", "fg", "a",
    "q", "Q", "qt", "t", "T", "d", "w", "W", "c", "z", "x", "v", "g", "k", "o", "i", "O", "j", "p",
    "u", "P", "h", "hk", "ho", "hl", "y", "n", "nj", "np", "nl", "b", "m", "ml", "l",
];

impl VmKeyboard {
    pub fn encode_char(&mut self, c: char) -> Option<Vec<ScanEvent>> {
        if let Some((scancode, shift)) = ascii_key(c) {
            let mut events = Vec::new();
            if c.is_ascii_alphabetic() && self.hangul {
                events.extend(hangul_toggle());
                self.hangul = false;
            }
            append_key(&mut events, scancode, shift);
            self.composing = false;
            return Some(events);
        }

        let keys = match c as u32 {
            0xAC00..=0xD7A3 => {
                let syllable = (c as u32 - 0xAC00) as usize;
                [
                    INITIALS[syllable / 588],
                    VOWELS[(syllable % 588) / 28],
                    FINALS[syllable % 28],
                ]
            }
            0x3131..=0x3163 => [JAMO[(c as u32 - 0x3131) as usize], "", ""],
            _ => {
                self.composing = false;
                return None;
            }
        };
        let mut events = Vec::new();
        if !self.hangul {
            events.extend(hangul_toggle());
            self.hangul = true;
        }
        for key in keys.iter().flat_map(|keys| keys.bytes()) {
            append_key(
                &mut events,
                LETTER_SCANCODES[(key.to_ascii_lowercase() - b'a') as usize],
                key.is_ascii_uppercase(),
            );
        }
        self.composing = true;
        Some(events)
    }

    pub fn backspace(&mut self) -> Vec<ScanEvent> {
        let mut events = Vec::new();
        if self.composing {
            events.extend(hangul_toggle());
            events.extend(hangul_toggle());
        }
        append_key(&mut events, 0x0E, false);
        self.composing = false;
        events
    }

    pub fn reset_composition(&mut self) {
        self.composing = false;
    }
}

pub fn hangul_toggle() -> Vec<ScanEvent> {
    vec![
        ScanEvent {
            scancode: HANGUL_TOGGLE_SCANCODE,
            down: true,
        },
        ScanEvent {
            scancode: HANGUL_TOGGLE_SCANCODE,
            down: false,
        },
    ]
}

fn append_key(events: &mut Vec<ScanEvent>, scancode: u32, shift: bool) {
    if shift {
        events.push(ScanEvent {
            scancode: 0x2A,
            down: true,
        });
    }
    events.push(ScanEvent {
        scancode,
        down: true,
    });
    events.push(ScanEvent {
        scancode,
        down: false,
    });
    if shift {
        events.push(ScanEvent {
            scancode: 0x2A,
            down: false,
        });
    }
}

fn ascii_key(c: char) -> Option<(u32, bool)> {
    let scancode = match c {
        'a'..='z' | 'A'..='Z' => LETTER_SCANCODES[(c.to_ascii_lowercase() as u8 - b'a') as usize],
        '1' | '!' => 0x02,
        '2' | '@' => 0x03,
        '3' | '#' => 0x04,
        '4' | '$' => 0x05,
        '5' | '%' => 0x06,
        '6' | '^' => 0x07,
        '7' | '&' => 0x08,
        '8' | '*' => 0x09,
        '9' | '(' => 0x0A,
        '0' | ')' => 0x0B,
        '-' | '_' => 0x0C,
        '=' | '+' => 0x0D,
        '\t' => 0x0F,
        '[' | '{' => 0x1A,
        ']' | '}' => 0x1B,
        '\n' => 0x1C,
        ';' | ':' => 0x27,
        '\'' | '"' => 0x28,
        '`' | '~' => 0x29,
        '\\' | '|' => 0x2B,
        ',' | '<' => 0x33,
        '.' | '>' => 0x34,
        '/' | '?' => 0x35,
        ' ' => 0x39,
        _ => return None,
    };
    let shift = c.is_ascii_uppercase() || "!@#$%^&*()_+{}:\"~|<>?".contains(c);
    Some((scancode, shift))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strokes(keys: &[(u32, bool)]) -> Vec<ScanEvent> {
        let mut events = Vec::new();
        for &(scancode, shift) in keys {
            if shift {
                events.push(ScanEvent {
                    scancode: 0x2A,
                    down: true,
                });
            }
            events.push(ScanEvent {
                scancode,
                down: true,
            });
            events.push(ScanEvent {
                scancode,
                down: false,
            });
            if shift {
                events.push(ScanEvent {
                    scancode: 0x2A,
                    down: false,
                });
            }
        }
        events
    }

    #[test]
    fn ascii_examples() {
        for (c, scancode, shift) in [
            ('a', 0x1E, false),
            ('A', 0x1E, true),
            ('1', 0x02, false),
            ('!', 0x02, true),
            (' ', 0x39, false),
            ('\n', 0x1C, false),
            ('\t', 0x0F, false),
        ] {
            assert_eq!(
                VmKeyboard::default().encode_char(c),
                Some(strokes(&[(scancode, shift)]))
            );
        }
    }

    #[test]
    fn complete_us_layout() {
        let rows = [
            (
                "`1234567890-=",
                "~!@#$%^&*()_+",
                vec![
                    0x29, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
                ],
            ),
            (
                "qwertyuiop[]\\",
                "QWERTYUIOP{}|",
                vec![
                    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x2B,
                ],
            ),
            (
                "asdfghjkl;'",
                "ASDFGHJKL:\"",
                vec![
                    0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                ],
            ),
            (
                "zxcvbnm,./",
                "ZXCVBNM<>?",
                vec![0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35],
            ),
        ];
        let mut covered = vec![' '];
        for (plain, shifted, scancodes) in rows {
            for ((plain, shifted), scancode) in plain.chars().zip(shifted.chars()).zip(scancodes) {
                for (c, shift) in [(plain, false), (shifted, true)] {
                    assert_eq!(
                        VmKeyboard::default().encode_char(c),
                        Some(strokes(&[(scancode, shift)])),
                        "{c:?}"
                    );
                    covered.push(c);
                }
            }
        }
        covered.sort_unstable();
        assert_eq!(covered, (' '..='~').collect::<Vec<_>>());
    }

    #[test]
    fn hangul_then_ascii_letter() {
        let mut keyboard = VmKeyboard::default();
        assert_eq!(
            keyboard.encode_char('한'),
            Some(strokes(&[
                (0x72, false),
                (0x22, false),
                (0x25, false),
                (0x1F, false)
            ]))
        );
        assert_eq!(
            keyboard.encode_char('a'),
            Some(strokes(&[(0x72, false), (0x1E, false)]))
        );
        assert_eq!(keyboard.encode_char('A'), Some(strokes(&[(0x1E, true)])));
        assert!(!keyboard.hangul);
        assert!(!keyboard.composing);
    }

    #[test]
    fn hangul_syllables_and_compounds() {
        for (c, keys) in [
            ('까', vec![(0x13, true), (0x25, false)]),
            ('갈', vec![(0x13, false), (0x25, false), (0x21, false)]),
            (
                '값',
                vec![(0x13, false), (0x25, false), (0x10, false), (0x14, false)],
            ),
            ('과', vec![(0x13, false), (0x23, false), (0x25, false)]),
            ('가', vec![(0x13, false), (0x25, false)]),
            ('힣', vec![(0x22, false), (0x26, false), (0x22, false)]),
        ] {
            let mut expected = strokes(&[(0x72, false)]);
            expected.extend(strokes(&keys));
            let mut keyboard = VmKeyboard::default();
            assert_eq!(keyboard.encode_char(c), Some(expected), "{c}");
            assert_eq!(keyboard.encode_char(c), Some(strokes(&keys)), "{c}");
            assert!(keyboard.hangul && keyboard.composing);
        }
    }

    #[test]
    fn compatibility_jamo() {
        for (c, keys) in [
            ('ㅘ', vec![(0x23, false), (0x25, false)]),
            ('ㄳ', vec![(0x13, false), (0x14, false)]),
            ('ㄱ', vec![(0x13, false)]),
            ('ㄲ', vec![(0x13, true)]),
            ('ㅣ', vec![(0x26, false)]),
        ] {
            let mut expected = strokes(&[(0x72, false)]);
            expected.extend(strokes(&keys));
            let mut keyboard = VmKeyboard::default();
            assert_eq!(keyboard.encode_char(c), Some(expected), "{c}");
            assert!(keyboard.hangul && keyboard.composing);
        }
    }

    #[test]
    fn backspace_commits_composition_once() {
        let mut keyboard = VmKeyboard::default();
        assert_eq!(keyboard.backspace(), strokes(&[(0x0E, false)]));
        keyboard.encode_char('한').unwrap();
        assert_eq!(
            keyboard.backspace(),
            strokes(&[(0x72, false), (0x72, false), (0x0E, false)])
        );
        assert!(keyboard.hangul);
        assert!(!keyboard.composing);
        assert_eq!(keyboard.backspace(), strokes(&[(0x0E, false)]));
        assert_eq!(
            keyboard.encode_char('가'),
            Some(strokes(&[(0x13, false), (0x25, false)]))
        );
    }

    #[test]
    fn nonletters_clear_composition_without_toggling() {
        for (c, scancode, shift) in [
            ('1', 0x02, false),
            ('!', 0x02, true),
            (' ', 0x39, false),
            ('\n', 0x1C, false),
            ('\t', 0x0F, false),
        ] {
            let mut keyboard = VmKeyboard::default();
            keyboard.encode_char('한').unwrap();
            assert_eq!(keyboard.encode_char(c), Some(strokes(&[(scancode, shift)])));
            assert!(keyboard.hangul);
            assert!(!keyboard.composing);
            assert_eq!(keyboard.backspace(), strokes(&[(0x0E, false)]));
        }
    }

    #[test]
    fn unsupported_characters_clear_only_composition() {
        for c in [
            '😀', '漢', '\0', '\r', '\u{7F}', '\u{1100}', '\u{3130}', '\u{3164}', '\u{ABFF}',
            '\u{D7A4}',
        ] {
            let mut keyboard = VmKeyboard::default();
            assert_eq!(keyboard.encode_char(c), None);
            assert!(!keyboard.hangul);
            keyboard.encode_char('한').unwrap();
            assert_eq!(keyboard.encode_char(c), None);
            assert!(keyboard.hangul);
            assert!(!keyboard.composing);
            assert_eq!(keyboard.backspace(), strokes(&[(0x0E, false)]));
        }
    }

    #[test]
    fn reset_and_manual_toggle_preserve_mode() {
        let mut keyboard = VmKeyboard::default();
        keyboard.encode_char('한').unwrap();
        keyboard.reset_composition();
        assert!(keyboard.hangul);
        assert!(!keyboard.composing);
        assert_eq!(keyboard.backspace(), strokes(&[(0x0E, false)]));
        assert_eq!(hangul_toggle(), strokes(&[(0x72, false)]));
        assert_eq!(
            keyboard.encode_char('a'),
            Some(strokes(&[(0x72, false), (0x1E, false)]))
        );
    }
}

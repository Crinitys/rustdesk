use super::{has_hotkey_modifiers, sim_rdev_rawkey_position, ENIGO};
use base::message_proto::{key_event, ControlKey, KeyEvent};
use enigo::KeyboardControllable;
use hbb_common::protobuf::Enum;

#[cfg(windows)]
lazy_static::lazy_static! {
    static ref VM_KEYBOARD: std::sync::Mutex<crate::vm_keyboard::VmKeyboard> = Default::default();
}

#[cfg(windows)]
pub(super) fn vm_keyboard_input(evt: &KeyEvent) -> bool {
    if has_hotkey_modifiers(evt) {
        return false;
    }
    if !is_vm_console_foreground() {
        VM_KEYBOARD.lock().unwrap().reset_composition();
        return false;
    }
    match evt.union {
        Some(key_event::Union::Chr(c)) | Some(key_event::Union::Unicode(c)) => {
            if !evt.down {
                return false;
            }
            let Some(c) = char::from_u32(c) else {
                return false;
            };
            let events = VM_KEYBOARD.lock().unwrap().encode_char(c);
            if let Some(events) = events {
                for ev in events {
                    sim_rdev_rawkey_position(ev.scancode as _, ev.down);
                }
                true
            } else {
                false
            }
        }
        Some(key_event::Union::Seq(ref s)) => {
            for c in s.chars() {
                let events = VM_KEYBOARD.lock().unwrap().encode_char(c);
                if let Some(events) = events {
                    for ev in events {
                        sim_rdev_rawkey_position(ev.scancode as _, ev.down);
                    }
                } else {
                    ENIGO.lock().unwrap().key_sequence(&c.to_string());
                }
            }
            true
        }
        Some(key_event::Union::ControlKey(ck)) if ck.value() == ControlKey::Backspace.value() => {
            if evt.down {
                let events = VM_KEYBOARD.lock().unwrap().backspace();
                for ev in events {
                    sim_rdev_rawkey_position(ev.scancode as _, ev.down);
                }
            }
            true
        }
        Some(key_event::Union::ControlKey(_)) => {
            VM_KEYBOARD.lock().unwrap().reset_composition();
            false
        }
        _ => false,
    }
}

#[cfg(windows)]
pub fn is_vm_console_foreground() -> bool {
    use winapi::{
        shared::minwindef::DWORD,
        um::winuser::{GetForegroundWindow, GetWindowThreadProcessId},
    };

    let mut process_id: DWORD = 0;
    unsafe {
        if GetWindowThreadProcessId(GetForegroundWindow(), &mut process_id) == 0 {
            return false;
        }
    }
    if process_id == 0 {
        return false;
    }
    let Ok(path) = crate::platform::windows::get_process_executable_path(process_id) else {
        return false;
    };
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    [
        "vmware.exe",
        "vmplayer.exe",
        "vmconnect.exe",
        "virtualboxvm.exe",
    ]
    .iter()
    .any(|candidate| name.eq_ignore_ascii_case(candidate))
}

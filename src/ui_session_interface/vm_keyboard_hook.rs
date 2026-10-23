use super::{InvokeUiSession, Session};
use crate::client::{Data, Interface};
use base::message_proto::{KeyEvent, KeyboardMode, Message};

impl<T: InvokeUiSession> Session<T> {
    pub(super) fn vm_keyboard_input_key(&self, name: &str, alt: bool, ctrl: bool, command: bool) -> bool {
        use crate::vm_keyboard::{
            hangul_toggle, CLIENT, HANGUL_TOGGLE_KEY_NAME, OPTION_VM_KEYBOARD,
        };

        if name == HANGUL_TOGGLE_KEY_NAME {
            self.send_vm_keyboard_events(hangul_toggle());
            return true;
        }
        if self.get_option(OPTION_VM_KEYBOARD.to_owned()) != "Y"
            || self.peer_platform() != "Windows"
        {
            return false;
        }
        if alt || ctrl || command {
            CLIENT.lock().unwrap().reset_composition();
            return false;
        }
        let mut chars = name.chars();
        if let (Some(c), None) = (chars.next(), chars.next()) {
            let events = CLIENT.lock().unwrap().encode_char(c);
            if let Some(events) = events {
                self.send_vm_keyboard_events(events);
                return true;
            }
            return false;
        }
        if name == "VK_BACK" {
            let events = CLIENT.lock().unwrap().backspace();
            self.send_vm_keyboard_events(events);
            return true;
        }
        CLIENT.lock().unwrap().reset_composition();
        false
    }

    pub(super) fn vm_keyboard_input_string(&self, value: &str) -> bool {
        use crate::vm_keyboard::{CLIENT, OPTION_VM_KEYBOARD};

        if self.get_option(OPTION_VM_KEYBOARD.to_owned()) != "Y"
            || self.peer_platform() != "Windows"
        {
            return false;
        }
        for c in value.chars() {
            let events = CLIENT.lock().unwrap().encode_char(c);
            if let Some(events) = events {
                self.send_vm_keyboard_events(events);
            } else {
                let mut key_event = KeyEvent::new();
                key_event.set_seq(c.to_string());
                let mut msg_out = Message::new();
                msg_out.set_key_event(key_event);
                self.send(Data::Message(msg_out));
            }
        }
        true
    }

    fn send_vm_keyboard_events(&self, events: Vec<crate::vm_keyboard::ScanEvent>) {
        for event in events {
            let mut key_event = KeyEvent {
                mode: KeyboardMode::Map.into(),
                down: event.down,
                press: false,
                ..Default::default()
            };
            key_event.set_chr(event.scancode);
            self.send_key_event(&key_event);
        }
    }
}

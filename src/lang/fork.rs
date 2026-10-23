pub fn get(lang: &str, name: &str) -> Option<&'static str> {
    match (lang, name) {
        ("ko", "Three-finger scroll speed") => Some("세 손가락 스크롤 속도"),
        _ => None,
    }
}

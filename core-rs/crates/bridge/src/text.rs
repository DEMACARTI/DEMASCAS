//! Text helpers shared by the crates.

/// The longest prefix of `s` that is at most `max` bytes and ends on a
/// character boundary.  (The C++ daemon cut by bytes, split "✓"/"—" in
/// half, and crashed when the JSON library met the broken text.)
pub fn utf8_cut(s: &str, max: usize) -> &str {
    if s.len() <= max {
        return s;
    }
    let mut n = max;
    while !s.is_char_boundary(n) {
        n -= 1;
    }
    &s[..n]
}

#[cfg(test)]
mod tests {
    use super::utf8_cut;

    #[test]
    fn never_splits_a_character() {
        let s = "Done ✓ — café 👍 ok";
        for max in 0..=s.len() + 2 {
            let cut = utf8_cut(s, max);
            assert!(cut.len() <= max.max(0));
            assert!(s.starts_with(cut));
        }
        assert_eq!(utf8_cut("abc", 10), "abc");
        assert_eq!(utf8_cut("✓", 2), "");
    }
}

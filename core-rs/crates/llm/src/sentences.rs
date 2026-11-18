//! Streamed tokens → speakable sentences (ported from `split_sentence` in
//! `llm_client.cpp`).  A sentence goes to the voice the moment it ends, so
//! DEMASCAS starts talking while the model is still writing.

use std::collections::HashSet;

const MIN_SENTENCE: usize = 10;
const MIN_CLAUSE: usize = 30;

/// Split off the longest complete sentence (or, failing that, a long
/// clause ending in , ; :).  Returns (sentence, rest); sentence is "" if
/// nothing is complete yet.
pub fn split_sentence(buf: &str) -> (String, String) {
    let b = buf.as_bytes();
    let n = b.len();
    let (mut sentence_end, mut clause_end) = (None, None);
    for i in 0..n {
        match b[i] {
            b'\n' => {
                if i + 1 >= MIN_SENTENCE {
                    sentence_end = Some(i + 1);
                }
            }
            b'.' | b'!' | b'?' => {
                let mut pos = i + 1;
                if pos >= n || b[pos] == b' ' || b[pos] == b'\n' {
                    if pos < n {
                        pos += 1;
                    }
                    if pos >= MIN_SENTENCE {
                        sentence_end = Some(pos);
                    }
                }
            }
            b',' | b';' | b':' => {
                let pos = i + 1;
                if pos < n && b[pos] == b' ' && pos + 1 >= MIN_CLAUSE {
                    clause_end = Some(pos + 1);
                }
            }
            _ => {}
        }
    }
    // Every split point is just after an ASCII byte, so it's a char boundary.
    match sentence_end.or(clause_end) {
        None => (String::new(), buf.to_string()),
        Some(at) => (buf[..at].trim_end_matches([' ', '\n']).to_string(), buf[at..].to_string()),
    }
}

/// Collects tokens and hands out each sentence once — the same sentence is
/// never spoken twice in one answer (small models repeat themselves).
#[derive(Default)]
pub struct SentenceStream {
    buf: String,
    said: HashSet<String>,
}

fn key(s: &str) -> String {
    s.chars().filter(|c| c.is_alphanumeric()).flat_map(char::to_lowercase).collect()
}

impl SentenceStream {
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a token; returns a finished sentence, if one completed.
    pub fn push(&mut self, token: &str) -> Option<String> {
        self.buf.push_str(token);
        let (sentence, rest) = split_sentence(&self.buf);
        if sentence.is_empty() {
            return None;
        }
        self.buf = rest;
        self.said.insert(key(&sentence)).then_some(sentence)
    }

    /// Whatever is left when the stream ends.
    pub fn finish(&mut self) -> Option<String> {
        let rest = std::mem::take(&mut self.buf);
        let rest = rest.trim_end_matches([' ', '\n']);
        (!rest.trim().is_empty() && self.said.insert(key(rest))).then(|| rest.to_string())
    }
}

/// Small models fall into loops ("…on your slides. Still working on your
/// slides. Still working…").  The last 32 bytes seen 3+ times = a loop.
pub fn is_looping(text: &str) -> bool {
    if text.len() < 160 {
        return false;
    }
    let mut start = text.len() - 32;
    while !text.is_char_boundary(start) {
        start -= 1;
    }
    let tail = &text[start..];
    let mut count = 0;
    let mut from = 0;
    while let Some(p) = text[from..].find(tail) {
        count += 1;
        from += p + 1;
        while from < text.len() && !text.is_char_boundary(from) {
            from += 1;
        }
        if count >= 3 {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_like_the_cpp_version() {
        assert_eq!(split_sentence("Paris is the capital. It is"), ("Paris is the capital.".into(), "It is".into()));
        assert_eq!(split_sentence("Hi. "), (String::new(), "Hi. ".into()));     // < 10 chars
        assert_eq!(split_sentence("Version 2.5 is"), (String::new(), "Version 2.5 is".into())); // no space after '.'
        let long = "Well, after thinking about it carefully, the answer";
        assert_eq!(split_sentence(long).0, "Well, after thinking about it carefully,");
    }

    #[test]
    fn stream_speaks_each_sentence_once() {
        let mut s = SentenceStream::new();
        let mut out = vec![];
        for t in ["The capital ", "of France is Paris. ", "The capital of France is Paris. ", "Nice ", "city"] {
            out.extend(s.push(t));
        }
        out.extend(s.finish());
        assert_eq!(out, vec!["The capital of France is Paris.", "Nice city"]);
    }

    #[test]
    fn detects_loops_and_multibyte_is_safe() {
        let looped = "Still working on your slides. ".repeat(8);
        assert!(is_looping(&looped));
        assert!(!is_looping(&"A varied sentence number one. ".chars().chain("Then something else entirely, with more words. ".chars()).chain("And a final different ending here, okay! Done now.".chars()).collect::<String>()));
        let emoji = "✓ — 👍 ".repeat(40);
        let _ = is_looping(&emoji); // must not panic on char boundaries
    }
}

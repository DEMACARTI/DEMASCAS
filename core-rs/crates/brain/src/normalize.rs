//! Clean up what the recogniser heard before routing — the fixes a person
//! makes without noticing.

/// "Daksh, underscore, Rathore, underscore, CV" → "Daksh_Rathore_CV";
/// "report dot pdf" → "report.pdf"; "send high to Hatim" → "send hi to Hatim".
pub fn normalize(text: &str) -> String {
    let mut words: Vec<String> = text.split_whitespace().map(str::to_string).collect();
    let strip = |w: &str| w.trim_matches(|c: char| c == ',' || c == '.').to_lowercase();
    // spoken separators glue their neighbours together
    let mut i = 0;
    while i < words.len() {
        let w = strip(&words[i]);
        let sep = match w.as_str() {
            "underscore" | "underscores" => Some("_"),
            "dash" | "hyphen" => Some("-"),
            "dot" if i + 1 < words.len() && is_ext(&strip(&words[i + 1])) => Some("."),
            _ => None,
        };
        if let (Some(sep), true, true) = (sep, i > 0, i + 1 < words.len()) {
            let left = words[i - 1].trim_end_matches(',').to_string();
            let right = words[i + 1].trim_matches(',').to_string();
            words.splice(i - 1..=i + 1, [format!("{left}{sep}{right}")]);
            i -= 1;
            continue;
        }
        i += 1;
    }
    let mut out = join_spelled(&words.join(" "));
    // messaging mishearings ("hi" is the most common greeting by far)
    for (a, b) in [("send high", "send hi"), ("say high", "say hi"), ("send hii", "send hi"), ("send hay", "send hey")] {
        if let Some(p) = out.to_lowercase().find(a) {
            out.replace_range(p..p + a.len(), b);
        }
    }
    out
}

/// "V-I-J-V-A-L" / "V I J V A L" / "v. i. j. v. a. l." → "Vijval";
/// "spread by" / "spelled by" / "spelt" → "spelled" (live log 18 Nov).
fn join_spelled(text: &str) -> String {
    let letter = |w: &str| {
        let w = w.trim_matches(|c: char| c == ',' || c == '.');
        w.len() == 1 && w.chars().all(|c| c.is_ascii_alphabetic())
    };
    let cap = |letters: &str| {
        let l = letters.to_lowercase();
        let mut c = l.chars();
        c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
    };
    let words: Vec<&str> = text.split_whitespace().collect();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0;
    while i < words.len() {
        // one token: "V-I-J-V-A-L" / "V.I.J"
        let w = words[i].trim_end_matches([',', '.', '?', '!']);
        let parts: Vec<&str> = w.split(['-', '.']).filter(|p| !p.is_empty()).collect();
        if parts.len() >= 3 && parts.iter().all(|p| letter(p)) {
            out.push(cap(&parts.concat()));
            i += 1;
            continue;
        }
        // several tokens: "V I J V A L" (3+ single letters in a row)
        let mut j = i;
        while j < words.len() && letter(words[j]) {
            j += 1;
        }
        if j - i >= 3 {
            let letters: String = words[i..j].iter().map(|w| w.trim_matches(|c: char| c == ',' || c == '.')).collect();
            out.push(cap(&letters));
            i = j;
            continue;
        }
        out.push(words[i].to_string());
        i += 1;
    }
    let mut s = out.join(" ");
    for (a, b) in [("spread by ", "spelled "), ("spelled by ", "spelled "), ("spelt by ", "spelled "), ("spelled as ", "spelled "), ("spelt ", "spelled ")] {
        if let Some(p) = s.to_lowercase().find(a) {
            s.replace_range(p..p + a.len(), b);
        }
    }
    // "call the person whose name is spelled Vijval" → "call Vijval" (otherwise
    // the server takes "spelled X" as teaching a word and cuts the request)
    for a in ["the person whose name is spelled ", "someone whose name is spelled ", "the contact whose name is spelled ",
              "the person named ", "whose name is spelled "] {
        if let Some(p) = s.to_lowercase().find(a) {
            s.replace_range(p..p + a.len(), "");
        }
    }
    s
}

fn is_ext(w: &str) -> bool {
    ["pdf", "txt", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "png", "jpg", "jpeg", "mp4", "mp3", "zip", "key", "pages", "numbers", "md", "py", "json"].contains(&w)
}

/// A consonant skeleton — how a word SOUNDS, roughly.  "this card" and
/// "Discord" both become "dskrd"; "this cord" too.
pub fn skeleton(s: &str) -> String {
    let s: String = s.to_lowercase().chars().filter(|c| c.is_ascii_alphanumeric()).collect();
    let s = s.replace("th", "d").replace("ph", "f").replace("ck", "k").replace('c', "k").replace('q', "k").replace('z', "s").replace('x', "ks");
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        let keep = !"aeiouyhw".contains(ch) || i == 0 && "aeiou".contains(ch);
        let ch = if ch == 't' { 'd' } else if ch == 'b' { 'p' } else if ch == 'g' { 'k' } else if ch == 'v' { 'f' } else { ch };
        if keep && !out.ends_with(ch) {
            out.push(ch);
        }
    }
    out
}

/// A trailing comma, or a last word that needs more after it — the
/// speaker paused mid-sentence ("send hi, to…", "open Discord and").
pub fn sounds_unfinished(text: &str) -> bool {
    let t = text.trim();
    if t.ends_with(',') || t.ends_with('-') {
        return true;
    }
    let body = t.trim_end_matches(['.', '!', '?']).to_lowercase();
    // "…tell me what it is." / "where that is?" end on "is" but are whole sentences
    if ["what it is", "what that is", "what this is", "where it is", "who it is", "what is", "how it is", "it is", "that is", "this is"]
        .iter().any(|e| body.ends_with(e)) {
        return false;
    }
    let last = body.rsplit(' ').next().unwrap_or("").to_string();
    ["to", "and", "the", "a", "an", "on", "for", "with", "of", "at", "my", "in", "that", "then", "or", "is", "about", "from"].contains(&last.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whole_sentences_ending_in_is() {
        assert!(!sounds_unfinished("open that file path and tell me what it is."));
        assert!(sounds_unfinished("the file name is"));
        assert!(sounds_unfinished("send hi to Hatim and"));
    }

    #[test]
    fn spelled_names() {
        assert_eq!(normalize("now call the person whose name is spread by V-I-J-V-A-L."), "now call Vijval");
        assert_eq!(normalize("call V I J V A L"), "call Vijval");
        assert_eq!(normalize("call v. i. j. v. a. l."), "call Vijval");
        // ordinary single letters stay
        assert_eq!(normalize("I want a cup of tea"), "I want a cup of tea");
        assert_eq!(normalize("plan B is fine"), "plan B is fine");
    }

    #[test]
    fn spoken_file_names() {
        assert_eq!(normalize("the file name is, Daksh, underscore, Ratthor, underscore, CV."), "the file name is, Daksh_Ratthor_CV.");
        assert_eq!(normalize("open report dot pdf"), "open report.pdf");
        assert_eq!(normalize("connect the dot to the line"), "connect the dot to the line");
        assert_eq!(normalize("send high to Hatim on Discord"), "send hi to Hatim on Discord");
    }

    #[test]
    fn sounds_alike() {
        assert_eq!(skeleton("this card"), skeleton("Discord"));
        assert_eq!(skeleton("this cord"), skeleton("Discord"));
        assert_ne!(skeleton("Safari"), skeleton("Discord"));
    }

    #[test]
    fn unfinished() {
        assert!(sounds_unfinished("send high, do,"));
        assert!(sounds_unfinished("open Discord and"));
        assert!(!sounds_unfinished("open Discord."));
    }
}

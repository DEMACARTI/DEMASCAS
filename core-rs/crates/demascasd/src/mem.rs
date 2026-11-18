//! Memory pressure, in words.  On an 8 GB Mac the model's weights get
//! swapped out when other apps need RAM, and every reply then waits 8–20 s
//! while they're read back (live log 18 Nov: swap 4–4.8 GB).  Measured once
//! at startup and once at a wake when swap is high — never per request.

use std::collections::HashMap;
use std::sync::Mutex;

/// The biggest apps, refreshed in the background (never measured on the wake path).
static TOP: Mutex<Vec<(String, u64)>> = Mutex::new(Vec::new());

/// Keep `latest()` fresh: measure now, then every 5 minutes (one `top`, ~0.3 s, off-thread).
pub fn start_watch() {
    std::thread::spawn(|| loop {
        let v = top_apps(3);
        if let Ok(mut t) = TOP.lock() {
            *t = v;
        }
        std::thread::sleep(std::time::Duration::from_secs(300));
    });
}

/// The last measurement (empty until the first one finishes).
pub fn latest() -> Vec<(String, u64)> {
    TOP.lock().map(|t| t.clone()).unwrap_or_default()
}

/// Swap above this means replies will be slow.
pub const TIGHT_SWAP_MB: u64 = 2048;

/// The biggest apps by resident memory, helpers folded into their app
/// ("Brave Browser Helper (Renderer)" → "Brave Browser").  DEMASCAS's own
/// processes and the system are left out — they aren't the user's to close.
pub fn top_apps(n: usize) -> Vec<(String, u64)> {
    // `top`'s MEM is the real footprint, including compressed / swapped memory —
    // `ps` RSS showed Brave at 0.3 GB while its helpers really held ~1.3 GB (18 Nov)
    if let Some(v) = top_footprint(n) {
        return v;
    }
    let Ok(out) = std::process::Command::new("ps").args(["-axo", "rss=,comm="]).output() else { return Vec::new() };
    let mut by_app: HashMap<String, u64> = HashMap::new();
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        let line = line.trim();
        let Some((rss, path)) = line.split_once(' ') else { continue };
        let Ok(kb) = rss.trim().parse::<u64>() else { continue };
        if let Some(app) = app_name(path.trim()) {
            *by_app.entry(app).or_default() += kb / 1024;
        }
    }
    let mut v: Vec<(String, u64)> = by_app.into_iter().collect();
    v.sort_by(|a, b| b.1.cmp(&a.1));
    v.truncate(n);
    v
}

/// `top -l 1 -o mem` (~0.3 s): name → MB, helpers folded into their app.
fn top_footprint(n: usize) -> Option<Vec<(String, u64)>> {
    let out = std::process::Command::new("top").args(["-l", "1", "-o", "mem", "-n", "40", "-stats", "command,mem"]).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let mut by_app: HashMap<String, u64> = HashMap::new();
    let mut started = false;
    for line in text.lines() {
        if line.starts_with("COMMAND") {
            started = true;
            continue;
        }
        if !started {
            continue;
        }
        let line = line.trim_end();
        let Some(i) = line.rfind(char::is_whitespace) else { continue };
        let (name, mem) = (line[..i].trim(), line[i..].trim().trim_end_matches(['+', '-']));
        let Some(mb) = parse_mem(mem) else { continue };
        if let Some(app) = top_name(name) {
            *by_app.entry(app).or_default() += mb;
        }
    }
    let mut v: Vec<(String, u64)> = by_app.into_iter().collect();
    v.sort_by(|a, b| b.1.cmp(&a.1));
    v.truncate(n);
    (!v.is_empty()).then_some(v)
}

fn parse_mem(s: &str) -> Option<u64> {
    let (num, unit) = s.split_at(s.find(|c: char| c.is_ascii_alphabetic())?);
    let v: f64 = num.parse().ok()?;
    Some(match unit { "G" => (v * 1024.0) as u64, "M" => v as u64, "K" => (v / 1024.0) as u64, _ => 0 })
}

/// top's 16-char names → the app ("Brave Browser He" → "Brave Browser",
/// "2.1.327" → "claude" — Claude Code shows its version as its name).
fn top_name(name: &str) -> Option<String> {
    let base = name.split(" He").next().unwrap_or(name).trim();
    let base = if base.split('.').count() == 3 && base.chars().all(|c| c.is_ascii_digit() || c == '.') { "claude" } else { base };
    let lower = base.to_lowercase();
    const SKIP: &[&str] = &["python", "demascasd", "windowserver", "kernel_task", "launchd", "terminal", "iterm2", "finder", "dock", "coreaudiod"];
    (!base.is_empty() && !SKIP.iter().any(|s| lower.starts_with(s))).then(|| base.to_string())
}

fn app_name(path: &str) -> Option<String> {
    const SKIP: &[&str] = &[
        "python", "python3", "demascasd", "WindowServer", "kernel_task", "launchd", "mds", "mds_stores", "Finder",
        "Dock", "SystemUIServer", "loginwindow", "coreaudiod", "Terminal", "iTerm2",
    ];
    // the outermost ".app" in the path names the app a helper belongs to
    let name = match path.find(".app/") {
        Some(i) => path[..i].rsplit('/').next().unwrap_or(path).to_string(),
        None => {
            let base = path.rsplit('/').next().unwrap_or(path);
            // background servers the user may not know are running (mysqld, postgres)
            if path.starts_with("/System/") || path.starts_with("/usr/libexec") || path.starts_with("/usr/sbin") {
                return None;
            }
            base.to_string()
        }
    };
    (!SKIP.contains(&name.as_str()) && !name.is_empty()).then_some(name)
}

/// "Brave Browser (1.7 GB), mysqld (0.5 GB), WhatsApp (0.3 GB)".
pub fn describe(apps: &[(String, u64)]) -> String {
    apps.iter().map(|(a, mb)| format!("{a} ({:.1} GB)", *mb as f32 / 1024.0)).collect::<Vec<_>>().join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folds_helpers_and_skips_system() {
        assert_eq!(app_name("/Applications/Brave Browser.app/Contents/Frameworks/Brave Browser Framework.framework/Versions/1/Helpers/Brave Browser Helper (Renderer).app/Contents/MacOS/Brave Browser Helper (Renderer)").as_deref(), Some("Brave Browser"));
        assert_eq!(app_name("/usr/local/opt/mysql/bin/mysqld").as_deref(), Some("mysqld"));
        assert_eq!(app_name("/System/Library/CoreServices/WindowServer"), None);
        assert_eq!(app_name("python3"), None);
    }

    #[test]
    fn top_names_and_sizes() {
        assert_eq!(top_name("Brave Browser He").as_deref(), Some("Brave Browser"));
        assert_eq!(top_name("2.1.327").as_deref(), Some("claude"));
        assert_eq!(top_name("python3.13"), None);
        assert_eq!(parse_mem("3272M"), Some(3272));
        assert_eq!(parse_mem("1.2G"), Some(1228));
    }

    #[test]
    fn lists_something_quickly() {
        let t = std::time::Instant::now();
        let v = top_apps(3);
        assert!(!v.is_empty());
        assert!(t.elapsed() < std::time::Duration::from_millis(1500), "{:?}", t.elapsed());
    }
}

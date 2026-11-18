//! The DEMASCAS app's buttons (YES / NO / STOP) → the session, as if said.
//!
//! A tiny line protocol on 127.0.0.1:5002 — "<token> yes".  The token is a
//! fresh random secret written to ~/.demascas/app_token (owner-only, 0600) at
//! every start, so other software on this Mac can't approve anything; and
//! ONLY yes / no / stop are accepted, never commands.

use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc::Sender;

pub const ALLOWED: &[&str] = &["yes", "no", "stop"];

fn new_token() -> Option<String> {
    let mut b = [0u8; 24];
    std::fs::File::open("/dev/urandom").ok().and_then(|mut f| std::io::Read::read_exact(&mut f, &mut b).ok())?;
    let token: String = b.iter().map(|x| format!("{x:02x}")).collect();
    let dir = std::path::PathBuf::from(std::env::var("HOME").ok()?).join(".demascas");
    std::fs::create_dir_all(&dir).ok()?;
    let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(dir.join("app_token")).ok()?;
    f.write_all(token.as_bytes()).ok()?;
    Some(token)
}

/// "<token> yes" → Some("yes") for a valid token and an allowed word.
pub fn parse(line: &str, token: &str) -> Option<String> {
    let (t, cmd) = line.trim().split_once(' ')?;
    let cmd = cmd.trim().to_lowercase();
    (!token.is_empty() && t == token && ALLOWED.contains(&cmd.as_str())).then_some(cmd)
}

pub fn serve(tx: Sender<(String, String)>) {
    let Some(token) = new_token() else {
        eprintln!("[!] app control: couldn't write ~/.demascas/app_token — app buttons disabled");
        return;
    };
    tokio::spawn(async move {
        let listener = match tokio::net::TcpListener::bind("127.0.0.1:5002").await {
            Ok(l) => l,
            Err(e) => {
                eprintln!("[!] app control: port 5002 busy ({e}) — app buttons disabled");
                return;
            }
        };
        loop {
            let Ok((sock, _)) = listener.accept().await else { continue };
            let (tx, token) = (tx.clone(), token.clone());
            tokio::spawn(async move {
                let mut lines = BufReader::new(sock).lines();
                if let Ok(Ok(Some(line))) = tokio::time::timeout(std::time::Duration::from_secs(2), lines.next_line()).await {
                    if let Some(cmd) = parse(&line, &token) {
                        let _ = tx.send((cmd, "app".into())).await;
                    }
                }
            });
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_token_and_allowed_words() {
        assert_eq!(parse("abc yes", "abc").as_deref(), Some("yes"));
        assert_eq!(parse("abc STOP\n", "abc").as_deref(), Some("stop"));
        assert_eq!(parse("abc delete my files", "abc"), None);
        assert_eq!(parse("wrong yes", "abc"), None);
        assert_eq!(parse("abc yes", ""), None);
    }
}

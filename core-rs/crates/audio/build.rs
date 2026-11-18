// Link speexdsp (echo cancellation + resampling).  Found via pkg-config
// (Homebrew on macOS, libspeexdsp-dev / speexdsp on Linux).
fn main() {
    let out = std::process::Command::new("pkg-config").args(["--libs-only-L", "speexdsp"]).output();
    if let Ok(o) = out {
        for p in String::from_utf8_lossy(&o.stdout).split_whitespace() {
            if let Some(dir) = p.strip_prefix("-L") {
                println!("cargo:rustc-link-search=native={dir}");
            }
        }
    }
    for dir in ["/opt/homebrew/lib", "/usr/local/lib"] {
        if std::path::Path::new(dir).exists() {
            println!("cargo:rustc-link-search=native={dir}");
        }
    }
    println!("cargo:rustc-link-lib=dylib=speexdsp");
}

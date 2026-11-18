// Bindings are generated from the sherpa-onnx headers that ship with
// DEMASCAS (models/sherpa-onnx, downloaded by scripts/build.sh), so
// struct layouts always match the library actually linked.
use std::path::PathBuf;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let dir = std::env::var("DEMASCAS_SHERPA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| manifest.join("../../../models/sherpa-onnx"));
    let dir = dir.canonicalize().unwrap_or(dir);
    let header = dir.join("include/sherpa-onnx/c-api/c-api.h");
    let lib = dir.join("lib");
    println!("cargo:rerun-if-changed={}", header.display());
    println!("cargo:rerun-if-env-changed=DEMASCAS_SHERPA_DIR");
    if !header.exists() {
        panic!("sherpa-onnx not found at {} — run ./scripts/build.sh (or set DEMASCAS_SHERPA_DIR)", dir.display());
    }
    let bindings = bindgen::Builder::default()
        .header(header.to_string_lossy())
        .allowlist_function("SherpaOnnx.*")
        .allowlist_type("SherpaOnnx.*")
        .layout_tests(false)
        .generate()
        .expect("bindgen failed on sherpa-onnx c-api.h");
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    bindings.write_to_file(out.join("bindings.rs")).unwrap();
    println!("cargo:rustc-link-search=native={}", lib.display());
    println!("cargo:rustc-link-lib=dylib=sherpa-onnx-c-api");
    // this crate's tests; binaries add the same rpath from DEP_SHERPA_ONNX_C_API_LIBDIR
    println!("cargo:rustc-link-arg=-Wl,-rpath,{}", lib.display());
    println!("cargo:libdir={}", lib.display());
}

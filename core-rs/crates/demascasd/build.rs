// Find libsherpa-onnx-c-api at run time (its folder comes from the sherpa crate).
fn main() {
    if let Ok(dir) = std::env::var("DEP_SHERPA_ONNX_C_API_LIBDIR") {
        println!("cargo:rustc-link-arg=-Wl,-rpath,{dir}");
    }
}

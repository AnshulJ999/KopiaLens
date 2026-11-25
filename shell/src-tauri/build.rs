fn main() {
    // tauri-build embeds the app icon but only reruns on tauri.conf.json changes.
    println!("cargo:rerun-if-changed=icons");
    tauri_build::build()
}

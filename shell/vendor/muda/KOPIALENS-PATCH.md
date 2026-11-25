# muda 0.20.0 with one upstream fix

This is muda 0.20.0 from crates.io plus the Windows part of [muda PR #430](https://github.com/tauri-apps/muda/pull/430), released in muda 0.21.2. Without it, tray menu clicks stop working once a process has created about 64.5k menu items.

Changed files: `src/platform_impl/windows/mod.rs` and `src/platform_impl/windows/util.rs`. The `shell/src-tauri/Cargo.toml` `[patch.crates-io]` entry selects this copy.

Remove this folder and that entry once Tauri depends on muda 0.21.2 or newer.

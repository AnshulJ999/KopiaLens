//! `klens` / `kopialens-cli`: runs `<install>/server/kopialens-server --cli <args>` and passes its
//! exit code through. It lives in `<install>/bin`, the only folder the installer adds to PATH.
//! `--installer-path` is the installer's own PATH edit (installer_path.rs).

#[cfg(windows)]
mod installer_path;

use std::path::PathBuf;
use std::process::{exit, Command};

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn SetConsoleCtrlHandler(handler: Option<unsafe extern "system" fn(u32) -> i32>, add: i32) -> i32;
}

/// Ctrl+C reaches the server process too (same console); the launcher waits for it to finish
/// instead of returning the prompt first. A handler, unlike ignoring, is not inherited.
#[cfg(windows)]
unsafe extern "system" fn wait_for_child(_ctrl: u32) -> i32 {
    1
}

fn server() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let name = if cfg!(windows) { "kopialens-server.exe" } else { "kopialens-server" };
    let path = exe.parent()?.parent()?.join("server").join(name);
    path.is_file().then_some(path)
}

fn main() {
    #[cfg(windows)]
    {
        let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
        if args.first().is_some_and(|a| a == "--installer-path") {
            exit(installer_path::run(&args[1..]));
        }
    }
    let Some(server) = server() else {
        eprintln!("klens: the KopiaLens server program isn't next to this one (expected ..\\server). Reinstall KopiaLens.");
        exit(1);
    };
    #[cfg(windows)]
    unsafe {
        SetConsoleCtrlHandler(Some(wait_for_child), 1);
    }
    match Command::new(&server).arg("--cli").args(std::env::args_os().skip(1)).status() {
        Ok(status) => exit(status.code().unwrap_or(1)),
        Err(err) => {
            eprintln!("klens: couldn't start {}: {err}", server.display());
            exit(1);
        }
    }
}

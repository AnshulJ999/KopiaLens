// One desktop app per data folder. The first copy holds an exclusive lock on `<data>/desktop.lock`
// and listens on a loopback port named in `<data>/desktop-instance.json` with a random token. A
// later launch with the same data folder asks it to open its window (or quit) and exits; copies
// with different data folders (installed + portable) run side by side.
use std::fs::{File, OpenOptions, TryLockError};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const LOCK_FILE: &str = "desktop.lock";
const INFO_FILE: &str = "desktop-instance.json";

static EXITING: AtomicBool = AtomicBool::new(false);

/// This copy is quitting or restarting: a new launch is told so and waits for the lock instead of
/// handing its request to a copy that is about to exit.
pub fn exiting() {
    EXITING.store(true, Ordering::SeqCst);
}

/// Held for the whole run: the OS releases the lock when the process ends, even after a crash.
pub struct Instance {
    lock: Option<File>,
    listener: Option<TcpListener>,
    token: String,
}

pub enum Claim {
    First(Instance),
    /// Another copy runs on this data folder and took the request.
    Handed,
    /// Another copy holds this data folder but did not answer.
    Unreachable,
}

pub fn claim(data_dir: &Path, token: String, quit: bool) -> Claim {
    let _ = std::fs::create_dir_all(data_dir);
    let file = OpenOptions::new().create(true).truncate(false).write(true).open(data_dir.join(LOCK_FILE));
    let lock = match file {
        Ok(f) => match f.try_lock() {
            Ok(()) => Some(f),
            Err(TryLockError::WouldBlock) => match hand_over(&f, data_dir, quit) {
                Some(claim) => return claim,
                None => Some(f),   // the other copy exited meanwhile (a restart or an update)
            },
            // A folder that cannot be locked (some network shares): run without the guard.
            Err(TryLockError::Error(_)) => None,
        },
        Err(_) => None,   // an unwritable data folder: the server reports that itself
    };
    let listener = TcpListener::bind(("127.0.0.1", 0)).ok();
    if let Some(port) = listener.as_ref().and_then(|l| l.local_addr().ok()).map(|a| a.port()) {
        let info = data_dir.join(INFO_FILE);
        let tmp = info.with_extension("json.tmp");
        let body = json!({"port": port, "token": token, "pid": std::process::id()}).to_string();
        if std::fs::write(&tmp, body).is_ok() {
            let _ = std::fs::rename(&tmp, &info);
        }
    }
    Claim::First(Instance { lock, listener, token })
}

/// Sends "open" or "quit" to the copy holding the lock. It may still be starting, so keep trying
/// for a few seconds; a copy that is quitting (Quit waits up to a minute for an index to save) is
/// waited for. None = the lock came free and is now ours.
fn hand_over(lock: &File, data_dir: &Path, quit: bool) -> Option<Claim> {
    let cmd = if quit { "quit" } else { "open" };
    let start = Instant::now();
    let mut end = start + Duration::from_secs(10);
    while Instant::now() < end {
        match send(data_dir, cmd).as_deref() {
            Some("ok") => return Some(Claim::Handed),
            Some("exiting") => end = start + Duration::from_secs(90),
            _ => {}
        }
        if lock.try_lock().is_ok() {
            return None;
        }
        thread::sleep(Duration::from_millis(400));
    }
    Some(Claim::Unreachable)
}

/// The running copy's one-word reply ("ok" or "exiting"), or None when it did not answer.
fn send(data_dir: &Path, cmd: &str) -> Option<String> {
    let info = std::fs::read_to_string(data_dir.join(INFO_FILE)).ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())?;
    let port = info.get("port").and_then(Value::as_u64)?;
    let token = info.get("token").and_then(Value::as_str)?;
    let addr = SocketAddr::from(([127, 0, 0, 1], port as u16));
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    writeln!(stream, "{token} {cmd}").ok()?;
    let mut reply = String::new();
    BufReader::new(stream).read_line(&mut reply).ok()?;
    Some(reply.trim().to_string())
}

impl Instance {
    /// Answers later launches on a background thread. `on_request` gets "open" or "quit".
    pub fn serve(self, on_request: impl Fn(&str) + Send + 'static) {
        let Instance { lock, listener, token } = self;
        let Some(listener) = listener else {
            std::mem::forget(lock);   // keep holding the folder for the whole run
            return;
        };
        thread::spawn(move || {
            let _lock = lock;
            for mut stream in listener.incoming().flatten() {
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let Ok(clone) = stream.try_clone() else { continue };
                let mut line = String::new();
                if BufReader::new(clone.take(256)).read_line(&mut line).is_err() {
                    continue;
                }
                let mut parts = line.split_whitespace();
                if parts.next() != Some(token.as_str()) {
                    continue;
                }
                let cmd = parts.next().unwrap_or("");
                if cmd != "open" && cmd != "quit" {
                    continue;
                }
                if EXITING.load(Ordering::SeqCst) {
                    let _ = writeln!(stream, "exiting");
                } else {
                    let _ = writeln!(stream, "ok");
                    on_request(cmd);
                }
            }
        });
    }
}

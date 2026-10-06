// KopiaLens desktop app. Owns the tray and the server process; windows are views onto the local
// server, so closing them leaves the tray, the server and scheduled indexing running.
mod control;
mod server;
mod tray;

use std::sync::atomic::{AtomicBool, Ordering};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use server::{ApiError, Server};
use tray::Health;

pub struct Shell {
    pub server: Server,
    pub state: Mutex<Option<Value>>,
    pub health: Mutex<Health>,
    pub tray_click: Mutex<String>,
    pub menu_sig: Mutex<String>,
    pub seen: Mutex<tray::Seen>,
    started: Instant,
    quitting: AtomicBool,
}

fn shell(app: &AppHandle) -> Arc<Shell> {
    app.state::<Arc<Shell>>().inner().clone()
}

fn encode(v: &str) -> String {
    v.bytes()
        .map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") })
        .collect()
}

fn same_origin(url: &Url, port: u16) -> bool {
    matches!(url.host_str(), Some("127.0.0.1") | Some("localhost")) && url.port() == Some(port)
}

fn open_external(url: &str) {
    let _ = tauri_plugin_opener::open_url(url, None::<&str>);
}

// The newest finished download, for the page's "Show in folder" button.
static LAST_DOWNLOAD: Mutex<Option<PathBuf>> = Mutex::new(None);

fn on_download(webview: tauri::Webview, event: DownloadEvent<'_>) -> bool {
    if let DownloadEvent::Finished { path, success, .. } = event {
        let name = path.as_ref().and_then(|p| p.file_name()).map(|n| n.to_string_lossy().into_owned());
        let folder = path.as_ref().and_then(|p| p.parent()).and_then(|p| p.file_name())
            .map(|n| n.to_string_lossy().into_owned());
        if success {
            *LAST_DOWNLOAD.lock().unwrap() = path.clone();
        }
        let detail = json!({ "ok": success, "name": name, "folder": folder.unwrap_or_else(|| "Downloads".into()) });
        let _ = webview.eval(&format!(
            "window.dispatchEvent(new CustomEvent('kl-downloaded', {{ detail: {detail} }}))"));
    }
    true
}

/// Opens (or focuses) the main window, or one repository's own window.
pub fn open_window(app: &AppHandle, repo: Option<String>) {
    let sh = shell(app);
    let label = match &repo {
        Some(r) => format!("repo-{}", r.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '_' }).collect::<String>()),
        None => "main".to_string(),
    };
    if let Some(w) = app.get_webview_window(&label) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let mut next = "/".to_string();
    let mut title = "KopiaLens".to_string();
    if let Some(r) = &repo {
        next.push_str(&format!("?repo={}", encode(r)));
        let name = sh.state.lock().unwrap().as_ref()
            .and_then(|st| st.get("repos")?.as_array()?.iter()
                .find(|x| x.get("repo").and_then(Value::as_str) == Some(r.as_str()))?
                .get("name")?.as_str().map(str::to_string));
        title = format!("KopiaLens - {}", name.unwrap_or_else(|| format!("Repo {r}")));
    }
    // A one-time code turns into the window cookie the "This window only" access mode asks for.
    let url = match sh.server.post("/api/shell/open-code", json!({})).ok()
        .and_then(|v| v.get("code").and_then(Value::as_str).map(str::to_string)) {
        Some(code) => format!("{}/shell/open?code={}&next={}", sh.server.url(), encode(&code), encode(&next)),
        None => format!("{}{next}", sh.server.url()),
    };
    let Ok(parsed) = Url::parse(&url) else { return };
    let port = sh.server.port;
    let (app2, label2) = (app.clone(), label.clone());
    let mut builder = WebviewWindowBuilder::new(app, &label, WebviewUrl::External(parsed));
    if let Some(p) = &sh.server.portable {
        // A portable copy keeps its sign-in cookies with its data, not in the user profile.
        builder = builder.data_directory(p.join("webview"));
    }
    let built = builder
        .title(title)
        .inner_size(1440.0, 900.0)
        .min_inner_size(820.0, 560.0)
        .on_download(on_download)
        .on_navigation(move |u| {
            if same_origin(u, port) && u.query_pairs().any(|(k, _)| k == "kl-reveal-download") {
                if let Some(p) = LAST_DOWNLOAD.lock().unwrap().clone() {
                    let _ = tauri_plugin_opener::reveal_item_in_dir(p);
                }
                return false;
            }
            if same_origin(u, port) || u.scheme() == "about" {
                return true;
            }
            open_external(u.as_str());
            false
        })
        .on_new_window(move |u, _features| {
            // Same-origin popups are exports: load them in place, where they download. The Help
            // menu's "Open in browser" marks its link to leave the window instead.
            if same_origin(&u, port) && u.query_pairs().any(|(k, _)| k == "open-in-browser") {
                open_external(u.as_str());
            } else if same_origin(&u, port) {
                if let Some(w) = app2.get_webview_window(&label2) {
                    let _ = w.navigate(u);
                }
            } else {
                open_external(u.as_str());
            }
            NewWindowResponse::Deny
        })
        .build();
    if let Err(e) = built {
        sh.server.log(&format!("could not open window {label}: {e}"));
    }
}

fn quit_stops_indexing(sh: &Shell) -> bool {
    let state = sh.state.lock().unwrap().clone();
    match state.as_ref().and_then(|s| s.get("prefs")?.get("quit_indexing")?.as_str().map(str::to_string)) {
        Some(v) => v == "stop",
        None => sh.server.setting("quit_indexing").and_then(|v| v.as_str().map(|s| s == "stop")).unwrap_or(false),
    }
}

/// Stops the server (and, per Settings, running index workers) and exits. With `confirm`, asks
/// first when an index is running; `stop_all` always ends the workers (installer upgrades).
pub fn quit(app: &AppHandle, confirm: bool, stop_all: bool) {
    let app = app.clone();
    thread::spawn(move || {
        let sh = shell(&app);
        let stop = stop_all || quit_stops_indexing(&sh);
        if confirm {
            let running: Vec<String> = sh.state.lock().unwrap().as_ref()
                .and_then(|st| st.get("repos")?.as_array().cloned()).unwrap_or_default().iter()
                .filter(|r| r.get("running").and_then(Value::as_bool).unwrap_or(false))
                .filter_map(|r| r.get("name").and_then(Value::as_str).map(str::to_string))
                .collect();
            if !running.is_empty() {
                let who = running.join(", ");
                let msg = if stop {
                    format!("{who} is still indexing. Quitting stops it, and it runs again later.")
                } else {
                    format!("{who} is still indexing. It keeps going in the background and finishes on its own.")
                };
                let ok = app.dialog().message(msg).title("Quit KopiaLens?")
                    .kind(MessageDialogKind::Warning)
                    .buttons(MessageDialogButtons::OkCancelCustom("Quit".into(), "Cancel".into()))
                    .blocking_show();
                if !ok {
                    return;
                }
            }
        }
        if sh.quitting.swap(true, Ordering::SeqCst) {
            return;
        }
        sh.server.log("quit");
        if stop {
            sh.server.stop_workers();
        }
        sh.server.stop();
        app.exit(0);
    });
}

/// Runs a server action off the UI thread, toasts a failure, then refreshes the tray.
fn act(app: &AppHandle, fail_title: &'static str, path: String, body: Value) {
    let app = app.clone();
    thread::spawn(move || {
        let sh = shell(&app);
        if let Err(e) = sh.server.post(&path, body) {
            let detail = match e {
                ApiError::Down => "KopiaLens isn't running.".to_string(),
                ApiError::Unauthorized => "Restart KopiaLens from the tray, then try again.".to_string(),
                ApiError::Failed(d) => d,
            };
            tray::toast(&app, fail_title, &detail);
        }
        poll_once(&app);
    });
}

pub fn on_menu(app: &AppHandle, id: &str) {
    let sh = shell(app);
    let parts: Vec<&str> = id.splitn(3, ':').collect();
    match parts.as_slice() {
        ["open"] => open_window(app, None),
        ["browser"] => open_external(&format!("{}/", sh.server.url())),
        ["update"] => {
            let app = app.clone();
            thread::spawn(move || {
                let st = shell(&app).server.get("/api/shell/state").ok();
                if let Some(url) = st.as_ref().and_then(|v| v.get("update")).and_then(|u| u.get("url")).and_then(Value::as_str) {
                    open_external(url);
                }
            });
        }
        ["repo", "open", key] => open_window(app, Some(key.to_string())),
        ["repo", "index", key] => act(app, "Couldn't start indexing", format!("/api/index/build?repo={}", encode(key)), json!({})),
        ["repo", "stop", key] => act(app, "Couldn't stop indexing", format!("/api/index/stop?repo={}", encode(key)), json!({})),
        ["pause", hours] => act(app, "Couldn't pause indexing", "/api/index/pause".into(), json!({"hours": hours.parse::<u32>().unwrap_or(0)})),
        ["resume"] => act(app, "Couldn't resume indexing", "/api/index/resume".into(), json!({})),
        ["notify", level] => act(app, "Couldn't change notifications", "/api/shell/prefs".into(), json!({"notifications": level})),
        ["autostart"] => {
            let auto = app.autolaunch();
            let res = if auto.is_enabled().unwrap_or(false) { auto.disable() } else { auto.enable() };
            if let Err(e) = res {
                sh.server.log(&format!("start at logon change failed: {e}"));
                tray::toast(app, "Couldn't change Start at logon", &e.to_string());
            }
            poll_once(app);
        }
        ["data"] => { let _ = tauri_plugin_opener::open_path(&sh.server.data_dir, None::<&str>); }
        ["logs"] => { let _ = tauri_plugin_opener::open_path(sh.server.data_dir.join("logs"), None::<&str>); }
        ["restart"] => {
            let app = app.clone();
            thread::spawn(move || {
                let sh = shell(&app);
                sh.server.log("tray: restart");
                if !sh.server.restart() {
                    tray::toast(&app, "Restart failed", "KopiaLens didn't start again. Check the logs folder.");
                }
                poll_once(&app);
            });
        }
        ["quit"] => quit(app, true, false),
        _ => {}
    }
}

pub fn poll_once(app: &AppHandle) {
    let sh = shell(app);
    let result = if server::port_open(sh.server.port) { sh.server.get("/api/shell/state") } else { Err(ApiError::Down) };
    let mut h = tray::status_from(&result);
    match &result {
        Ok(v) => {
            *sh.state.lock().unwrap() = Some(v.clone());
            tray::notify_changes(app, &sh, v);
        }
        Err(_) => *sh.state.lock().unwrap() = None,
    }
    if h == Health::Down && sh.server.child_pid().is_some() && sh.started.elapsed() < Duration::from_secs(90) {
        h = Health::Starting;
    }
    tray::refresh(app, &sh, h);
}

/// The first time the last window closes, say that KopiaLens keeps running in the tray.
fn tray_note_once(app: &AppHandle) {
    let Ok(dir) = app.path().app_config_dir() else { return };
    let flag = dir.join("tray-note-shown");
    if flag.exists() {
        return;
    }
    let _ = std::fs::create_dir_all(&dir);
    let _ = std::fs::write(&flag, b"1");
    tray::toast(app, "KopiaLens is still running", "Open it again from the tray icon. Scheduled indexing keeps running.");
}

fn env_flag_off(name: &str) -> bool {
    std::env::var(name).map(|v| v.trim() == "0").unwrap_or(false)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let hidden = std::env::args().any(|a| a == "--hidden");
    // `--quit` closes a running KopiaLens (the installer uses it before replacing files).
    let quit_only = std::env::args().any(|a| a == "--quit");
    let mut builder = tauri::Builder::default();
    // Must be first: a second launch opens the window instead of starting another app.
    // KOPIALENS_SINGLE_INSTANCE=0 lets an isolated test copy run beside the real one.
    if env_flag_off("KOPIALENS_SINGLE_INSTANCE") {
        if quit_only {
            return;
        }
    } else {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Off the main thread: building a webview inside the cross-process hand-off deadlocks both copies.
            let (app, quit_now) = (app.clone(), args.iter().any(|a| a == "--quit"));
            thread::spawn(move || {
                if quit_now {
                    quit(&app, false, true);
                } else {
                    open_window(&app, None);
                }
            });
        }));
    }
    let app = builder
        .plugin(
            tauri_plugin_window_state::Builder::new()
                .with_state_flags(
                    tauri_plugin_window_state::StateFlags::SIZE
                        | tauri_plugin_window_state::StateFlags::POSITION
                        | tauri_plugin_window_state::StateFlags::MAXIMIZED,
                )
                .build(),
        )
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--hidden"])))
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                let app = window.app_handle();
                let others = app.webview_windows().keys().filter(|l| *l != window.label()).count();
                if others == 0 {
                    tray_note_once(app);
                }
            }
        })
        .setup(move |app| {
            if quit_only {
                // Nothing was running, so there is nothing to close.
                std::process::exit(0);
            }
            let handle = app.handle().clone();
            let Some(server) = Server::locate(app.path().resource_dir().ok()) else {
                app.dialog()
                    .message("Couldn't find the KopiaLens server next to this app. Reinstall KopiaLens, or set KOPIALENS_HOME to its folder.")
                    .title("KopiaLens can't start")
                    .kind(MessageDialogKind::Error)
                    .show(move |_| handle.exit(1));
                return Ok(());
            };
            let sh = Arc::new(Shell {
                server,
                state: Mutex::new(None),
                health: Mutex::new(Health::Starting),
                tray_click: Mutex::new(String::new()),
                menu_sig: Mutex::new(String::new()),
                seen: Mutex::new(tray::Seen::default()),
                started: Instant::now(),
                quitting: AtomicBool::new(false),
            });
            app.manage(sh.clone());
            sh.server.log(&format!("--- desktop app started (data {}) ---", sh.server.data_dir.display()));
            sh.server.start();
            tray::create(&handle, &sh)?;
            control::spawn(handle.clone(), sh.clone());
            let poll_app = handle.clone();
            thread::spawn(move || loop {
                poll_once(&poll_app);
                thread::sleep(Duration::from_secs(3));
            });
            if !hidden {
                let open_app = handle.clone();
                let sh2 = sh.clone();
                thread::spawn(move || {
                    if sh2.server.wait_up(Duration::from_secs(90)) {
                        open_window(&open_app, None);
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the KopiaLens desktop app");
    app.run(|_app, event| {
        // Closing the last window keeps the app in the tray; only an explicit quit exits.
        if let RunEvent::ExitRequested { code: None, api, .. } = event {
            api.prevent_exit();
        }
    });
}

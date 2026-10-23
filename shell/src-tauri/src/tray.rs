// The tray icon: a status-coloured icon, a menu rebuilt from the server's status, and toasts
// when indexing or a job finishes.
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};
use tauri_plugin_autostart::ManagerExt as _;
use tauri_plugin_notification::NotificationExt;

use crate::server::ApiError;
use crate::Shell;

const TRAY_ID: &str = "kopialens";

#[derive(Clone, Copy, PartialEq)]
pub enum Health {
    Starting,
    Down,
    Limited,
    Ok,
    Busy,
    Paused,
    Error,
}

/// The tray icon choices (Settings > Desktop app), 64x64 RGBA built by packaging/build_logos.py,
/// each in a dark-taskbar and a light-taskbar (`-light`) version.
fn icon(name: &str, light: bool) -> Image<'static> {
    macro_rules! pick {
        ($($id:literal),*) => {
            match (name, light) {
                $(
                    ($id, false) => include_bytes!(concat!("../icons/tray/", $id, ".rgba")),
                    ($id, true) => include_bytes!(concat!("../icons/tray/", $id, "-light.rgba")),
                )*
                (_, true) => include_bytes!("../icons/tray/k3g-light.rgba"),
                _ => include_bytes!("../icons/tray/k3g.rgba"),
            }
        };
    }
    let px: &'static [u8] = pick!("k3g", "k6s", "k3a", "tile", "mono", "q3", "q1", "s3", "r4", "slice", "q");
    Image::new(px, 64, 64)
}

/// Whether the taskbar (Windows) or the system appearance (elsewhere) is light.
#[cfg(windows)]
fn light_taskbar(_app: &AppHandle) -> bool {
    use winreg::enums::HKEY_CURRENT_USER;
    winreg::RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey("Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize")
        .and_then(|k| k.get_value::<u32, _>("SystemUsesLightTheme"))
        .map(|v| v == 1)
        .unwrap_or(false)
}

#[cfg(not(windows))]
fn light_taskbar(app: &AppHandle) -> bool {
    app.webview_windows()
        .values()
        .next()
        .and_then(|w| w.theme().ok())
        .map(|t| t == tauri::Theme::Light)
        .unwrap_or(false)
}

/// Start at logon is on for THIS copy. The autostart plugin checks only the entry's name, which
/// every KopiaLens copy with the same product name shares (installed and portable).
pub fn autostart_on(app: &AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false) && own_autostart_entry(app)
}

#[cfg(windows)]
fn own_autostart_entry(app: &AppHandle) -> bool {
    use winreg::enums::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    let Ok(exe) = std::env::current_exe() else { return true };
    let exe = exe.display().to_string().to_lowercase();
    [HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE].into_iter().any(|hive| {
        winreg::RegKey::predef(hive)
            .open_subkey("Software\\Microsoft\\Windows\\CurrentVersion\\Run")
            .and_then(|k| k.get_value::<String, _>(&app.package_info().name))
            .map(|v| v.to_lowercase().contains(&exe))
            .unwrap_or(false)
    })
}

#[cfg(not(windows))]
fn own_autostart_entry(_app: &AppHandle) -> bool {
    true
}

/// macOS tints a template icon to match its menu bar; only the one-colour choice suits that.
fn as_template(choice: &str) -> bool {
    cfg!(target_os = "macos") && choice == "mono"
}

/// The tray icon shown now (choice + light/dark), so a change is applied once.
static ICON_SHOWN: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn span(secs: f64) -> String {
    let m = (secs.max(0.0) / 60.0).round() as i64;
    match m {
        0 => "less than a minute".into(),
        1..=59 => format!("{m} min"),
        60..=2879 => {
            let (h, rest) = (m / 60, m % 60);
            if rest == 0 || h >= 10 { format!("{h} h") } else { format!("{h} h {rest} min") }
        }
        _ => format!("{} days", m / 1440),
    }
}

fn ago(ts: Option<f64>) -> String {
    match ts {
        Some(t) if now() - t < 60.0 => "indexed just now".into(),
        Some(t) => format!("indexed {} ago", span(now() - t)),
        None => "not indexed yet".into(),
    }
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn repos(state: &Value) -> Vec<Value> {
    state.get("repos").and_then(Value::as_array).cloned().unwrap_or_default()
}

fn repo_key(r: &Value) -> String {
    match r.get("repo") {
        Some(Value::String(k)) => k.clone(),
        Some(v) => v.to_string(),
        None => String::new(),
    }
}

fn pct(r: &Value) -> Option<i64> {
    r.get("progress")?.get("pct")?.as_f64().map(|p| p.round() as i64)
}

fn repo_status(r: &Value) -> String {
    if r.get("running").and_then(Value::as_bool).unwrap_or(false) {
        return match pct(r) { Some(p) => format!("indexing {p}%"), None => "indexing".into() };
    }
    if !s(r, "error").is_empty() {
        return "indexing failed".into();
    }
    ago(r.get("last_index").and_then(Value::as_f64))
}

/// The repo's backups line: who runs them, and KopiaLens's own state when it does.
fn backups(state: &Value, key: &str) -> Option<Value> {
    state.get("backups")?.get("repos")?.get(key).cloned()
}

fn app_name(id: &str) -> &str {
    match id {
        "kopiaui" => "KopiaUI",
        "blinkdisk" => "BlinkDisk",
        "kopia" => "a kopia server",
        "kopialens" => "KopiaLens",
        _ => "nothing",
    }
}

fn backups_text(b: &Value) -> String {
    if s(b, "choice") == "kopialens" {
        return match s(b, "state") {
            "running" => match b.get("next_ts").and_then(Value::as_f64) {
                Some(t) if t > now() => format!("Backups: KopiaLens, next in {}", span(t - now())),
                _ => "Backups: KopiaLens".into(),
            },
            "starting" => "Backups: KopiaLens, starting".into(),
            "paused" => "Backups: paused".into(),
            "blocked" => format!("Backups: {} is running them", app_name(s(b, "engine"))),
            "failed" => "Backups: stopped by an error".into(),
            _ => "Backups: KopiaLens".into(),
        };
    }
    format!("Backups: {}", app_name(s(b, "engine")))
}

/// One list for every pause menu (indexing and backups), matching the app's. 0 = until resumed.
const PAUSE_CHOICES: [(u32, &str); 7] = [(1, "1 hour"), (4, "4 hours"), (12, "12 hours"), (24, "1 day"),
    (72, "3 days"), (168, "1 week"), (0, "Until I resume")];

fn backups_paused(state: &Value) -> Option<Option<f64>> {
    let p = state.get("backups")?.get("pause")?;
    if !p.get("on").and_then(Value::as_bool).unwrap_or(false) {
        return None;
    }
    Some(p.get("until").and_then(Value::as_f64))
}

fn paused(state: &Value) -> Option<Option<f64>> {
    let p = state.get("pause")?;
    if !p.get("paused").and_then(Value::as_bool).unwrap_or(false) {
        return None;
    }
    Some(p.get("until").and_then(Value::as_f64))
}

fn pause_text(until: Option<f64>) -> String {
    match until {
        Some(u) => format!("Indexing paused, {} left", span(u - now())),
        None => "Indexing paused until you resume".into(),
    }
}

pub fn health(state: &Value) -> Health {
    let rs = repos(state);
    if rs.iter().any(|r| r.get("running").and_then(Value::as_bool).unwrap_or(false)) {
        Health::Busy
    } else if rs.iter().any(|r| !s(r, "error").is_empty()) {
        Health::Error
    } else if paused(state).is_some() {
        Health::Paused
    } else {
        Health::Ok
    }
}

fn tooltip(h: Health, state: Option<&Value>) -> String {
    let detail = match (h, state) {
        (Health::Starting, _) => "starting".to_string(),
        (Health::Down, _) => "not running".to_string(),
        (Health::Limited, _) => "restart from this menu to show status".to_string(),
        (_, Some(st)) => {
            let rs = repos(st);
            if let Some(r) = rs.iter().find(|r| r.get("running").and_then(Value::as_bool).unwrap_or(false)) {
                format!("{} {}", s(r, "name"), repo_status(r))
            } else if let Some(r) = rs.iter().find(|r| !s(r, "error").is_empty()) {
                format!("indexing failed for {}", s(r, "name"))
            } else if let Some(until) = paused(st) {
                pause_text(until).to_lowercase()
            } else {
                "indexes up to date".to_string()
            }
        }
        _ => String::new(),
    };
    format!("KopiaLens - {detail}")
}

pub fn pref(state: Option<&Value>, key: &str, default: &str) -> String {
    state
        .and_then(|v| v.get("prefs"))
        .and_then(|p| p.get(key))
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_string()
}

fn build_menu(app: &AppHandle, h: Health, state: Option<&Value>) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;
    let sep = || PredefinedMenuItem::separator(app);
    menu.append(&MenuItem::with_id(app, "open", "Open KopiaLens", true, None::<&str>)?)?;
    // "This window only" turns browsers away, so the menu does not offer one.
    if state.and_then(|v| v.get("access")).and_then(Value::as_str) != Some("window") {
        menu.append(&MenuItem::with_id(app, "browser", "Open in browser", h != Health::Down, None::<&str>)?)?;
    }
    menu.append(&sep()?)?;

    match state {
        Some(st) => {
            for r in repos(st) {
                let key = repo_key(&r);
                let sub = Submenu::new(app, format!("{}  ·  {}", s(&r, "name"), repo_status(&r)), true)?;
                sub.append(&MenuItem::with_id(app, format!("repo:open:{key}"), "Open", true, None::<&str>)?)?;
                if s(&r, "kind") != "backblaze" {
                    if r.get("running").and_then(Value::as_bool).unwrap_or(false) {
                        sub.append(&MenuItem::with_id(app, format!("repo:stop:{key}"), "Stop indexing", true, None::<&str>)?)?;
                    } else {
                        sub.append(&MenuItem::with_id(app, format!("repo:index:{key}"), "Index now", true, None::<&str>)?)?;
                    }
                    if let Some(b) = backups(st, &key) {
                        sub.append(&PredefinedMenuItem::separator(app)?)?;
                        sub.append(&MenuItem::with_id(app, format!("status:backups:{key}"), backups_text(&b), false, None::<&str>)?)?;
                        if s(&b, "choice") == "kopialens" {
                            if b.get("paused").and_then(Value::as_bool).unwrap_or(false) {
                                sub.append(&MenuItem::with_id(app, format!("repo:bresume:{key}"), "Resume backups", true, None::<&str>)?)?;
                            } else {
                                let pm = Submenu::new(app, "Pause backups", true)?;
                                for (h, label) in PAUSE_CHOICES {
                                    pm.append(&MenuItem::with_id(app, format!("repo:bpause:{key}:{h}"), label, true, None::<&str>)?)?;
                                }
                                sub.append(&pm)?;
                            }
                        }
                    }
                }
                menu.append(&sub)?;
            }
            menu.append(&sep()?)?;
            match paused(st) {
                Some(until) => {
                    menu.append(&MenuItem::with_id(app, "status:paused", pause_text(until), false, None::<&str>)?)?;
                    menu.append(&MenuItem::with_id(app, "resume", "Resume indexing", true, None::<&str>)?)?;
                }
                None => {
                    let sub = Submenu::new(app, "Pause indexing", true)?;
                    for (h, label) in PAUSE_CHOICES {
                        sub.append(&MenuItem::with_id(app, format!("pause:{h}"), label, true, None::<&str>)?)?;
                    }
                    menu.append(&sub)?;
                }
            }
            match backups_paused(st) {
                Some(until) => {
                    let text = match until {
                        Some(u) => format!("Backups paused, {} left", span(u - now())),
                        None => "Backups paused until you resume".into(),
                    };
                    menu.append(&MenuItem::with_id(app, "status:bpaused", text, false, None::<&str>)?)?;
                    menu.append(&MenuItem::with_id(app, "bresume", "Resume backups", true, None::<&str>)?)?;
                }
                None => {
                    let sub = Submenu::new(app, "Pause backups", true)?;
                    for (h, label) in PAUSE_CHOICES {
                        sub.append(&MenuItem::with_id(app, format!("bpause:{h}"), label, true, None::<&str>)?)?;
                    }
                    menu.append(&sub)?;
                }
            }
            let level = pref(state, "notifications", "warnings");
            let sub = Submenu::new(app, "Notifications", true)?;
            for (v, label) in [("all", "All"), ("warnings", "Warnings and errors"), ("off", "Off")] {
                sub.append(&CheckMenuItem::with_id(app, format!("notify:{v}"), label, true, level == v, None::<&str>)?)?;
            }
            menu.append(&sub)?;
        }
        None => {
            let text = match h {
                Health::Starting => "Starting…",
                Health::Limited => "Status shows after a restart",
                _ => "KopiaLens isn't running",
            };
            menu.append(&MenuItem::with_id(app, "status:none", text, false, None::<&str>)?)?;
        }
    }
    let autostart = autostart_on(app);
    menu.append(&CheckMenuItem::with_id(app, "autostart", "Start at logon", true, autostart, None::<&str>)?)?;
    menu.append(&sep()?)?;
    menu.append(&MenuItem::with_id(app, "data", "Open data folder", true, None::<&str>)?)?;
    menu.append(&MenuItem::with_id(app, "logs", "Open logs folder", true, None::<&str>)?)?;
    menu.append(&sep()?)?;
    if let Some(v) = state.map(|st| s(st, "version")).filter(|v| !v.is_empty()) {
        menu.append(&MenuItem::with_id(app, "status:version", format!("KopiaLens {v}"), false, None::<&str>)?)?;
    }
    if let Some(u) = state.and_then(|st| st.get("update")).filter(|u| u.is_object()) {
        let (latest, progress) = (s(u, "latest"), u.get("install").map(|i| s(i, "state")).unwrap_or(""));
        let pct = u.get("install").and_then(|i| i.get("pct")).and_then(Value::as_i64);
        let busy = match progress {
            "waiting" => Some("Update waits for indexing and jobs".to_string()),
            "checking" => Some("Preparing update…".to_string()),
            "downloading" => Some(match pct { Some(p) => format!("Downloading update… {p}%"), None => "Downloading update…".into() }),
            "installing" => Some("Installing update…".to_string()),
            _ => None,
        };
        match busy {
            Some(text) => menu.append(&MenuItem::with_id(app, "status:update", text, false, None::<&str>)?)?,
            None if u.get("can_install").and_then(Value::as_bool) == Some(true) =>
                menu.append(&MenuItem::with_id(app, "update", format!("Install update {latest}"), true, None::<&str>)?)?,
            None => menu.append(&MenuItem::with_id(app, "update", format!("Update available: {latest}"), true, None::<&str>)?)?,
        }
    }
    menu.append(&MenuItem::with_id(app, "restart", "Restart", true, None::<&str>)?)?;
    menu.append(&MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?)?;
    Ok(menu)
}

/// Builds (or rebuilds) the tray icon. Left-click-shows-menu can only be set at build time.
pub fn create(app: &AppHandle, shell: &Arc<Shell>) -> tauri::Result<()> {
    let state = shell.state.lock().unwrap().clone();
    let click = pref(state.as_ref(), "tray_click", "window");
    *shell.tray_click.lock().unwrap() = click.clone();
    let h = *shell.health.lock().unwrap();
    shell.server.log(&format!("start at logon: {}", autostart_on(app)));
    let menu = build_menu(app, h, state.as_ref())?;
    drop(app.remove_tray_by_id(TRAY_ID));
    let choice = pref(state.as_ref(), "tray_icon", "k3g");
    let light = light_taskbar(app);
    *ICON_SHOWN.lock().unwrap() = format!("{choice}|{light}");
    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon(&choice, light))
        .icon_as_template(as_template(&choice))
        .tooltip(tooltip(h, state.as_ref()))
        .menu(&menu)
        .show_menu_on_left_click(click == "menu")
        .on_menu_event(|app, event| crate::on_menu(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| {
            let app = tray.app_handle();
            let shell = app.state::<Arc<Shell>>();
            let state = shell.state.lock().unwrap().clone();
            match event {
                TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } => {
                    crate::note_action(app, "tray click");
                    if pref(state.as_ref(), "tray_click", "window") == "window" {
                        crate::open_window(app, None);
                    }
                }
                TrayIconEvent::DoubleClick { button: MouseButton::Left, .. } => {
                    crate::note_action(app, "tray double-click");
                    if pref(state.as_ref(), "tray_double_click", "window") == "window" {
                        crate::open_window(app, None);
                    }
                }
                _ => {}
            }
        })
        .build(app)?;
    *shell.menu_sig.lock().unwrap() = String::new();
    Ok(())
}

/// Applies a fresh status: icon, tooltip and menu change only when something visible changed.
pub fn refresh(app: &AppHandle, shell: &Arc<Shell>, h: Health) {
    let state = shell.state.lock().unwrap().clone();
    if pref(state.as_ref(), "tray_click", "window") != *shell.tray_click.lock().unwrap() {
        *shell.health.lock().unwrap() = h;
        if let Err(e) = create(app, shell) {
            shell.server.log(&format!("tray rebuild failed: {e}"));
        }
        return;
    }
    let Some(tray) = app.tray_by_id(TRAY_ID) else { return };
    let choice = pref(state.as_ref(), "tray_icon", "k3g");
    let light = light_taskbar(app);
    {
        let key = format!("{choice}|{light}");
        let mut shown = ICON_SHOWN.lock().unwrap();
        if *shown != key {
            let _ = tray.set_icon(Some(icon(&choice, light)));
            let _ = tray.set_icon_as_template(as_template(&choice));
            *shown = key;
        }
    }
    let tip = tooltip(h, state.as_ref());
    let menu_view = state.as_ref().map(|st| {
        let rows: Vec<String> = repos(st).iter().map(|r| {
            let key = repo_key(r);
            let b = backups(st, &key).map(|b| format!("{}|{}", backups_text(&b), b.get("paused").and_then(Value::as_bool).unwrap_or(false))).unwrap_or_default();
            format!("{}|{}|{}|{}", key, s(r, "name"), repo_status(r), b)
        }).collect();
        let bp = backups_paused(st).map(|u| u.map(|t| ((t - now()) / 60.0).round() as i64));
        json!([rows, paused(st).map(|u| pause_text(u)), bp, pref(Some(st), "notifications", "warnings"), s(st, "version"), s(st, "access"), st.get("update").cloned()]).to_string()
    });
    let autostart = autostart_on(app);
    let sig = format!("{:?}|{}|{}", menu_view, h as u8, autostart);
    *shell.health.lock().unwrap() = h;
    let _ = tray.set_tooltip(Some(tip));
    let mut prev = shell.menu_sig.lock().unwrap();
    if *prev != sig {
        match build_menu(app, h, state.as_ref()) {
            Ok(menu) => {
                let _ = tray.set_menu(Some(menu));
                *prev = sig;
            }
            Err(e) => shell.server.log(&format!("tray menu build failed: {e}")),
        }
    }
}

#[derive(Default)]
pub struct Seen {
    ready: bool,
    running: HashMap<String, bool>,
    jobs: HashMap<String, String>,
    alert: i64,
}

pub fn toast(app: &AppHandle, title: &str, body: &str) {
    let _ = app.notification().builder().title(title).body(body).show();
}

fn short(text: &str) -> String {
    let t: String = text.chars().take(140).collect();
    if t.len() < text.len() { format!("{t}…") } else { t }
}

/// Toasts for what finished since the last poll. The first poll only records.
pub fn notify_changes(app: &AppHandle, shell: &Arc<Shell>, state: &Value) {
    let level = pref(Some(state), "notifications", "warnings");
    let mut seen = shell.seen.lock().unwrap();
    let mut out: Vec<(bool, String, String)> = vec![];
    for r in repos(state) {
        let key = repo_key(&r);
        let running = r.get("running").and_then(Value::as_bool).unwrap_or(false);
        if seen.ready && seen.running.get(&key) == Some(&true) && !running {
            let err = s(&r, "error");
            if err.is_empty() {
                out.push((false, "Indexing finished".into(), s(&r, "name").into()));
            } else {
                out.push((true, "Indexing failed".into(), short(&format!("{}: {err}", s(&r, "name")))));
            }
        }
        seen.running.insert(key, running);
    }
    for j in state.get("jobs").and_then(Value::as_array).cloned().unwrap_or_default() {
        let id = s(&j, "job_id").to_string();
        let st = s(&j, "state").to_string();
        if seen.ready && seen.jobs.get(&id).map(String::as_str) == Some("running") {
            let what = if s(&j, "kind_label").is_empty() { "Job" } else { s(&j, "kind_label") };
            match st.as_str() {
                "done" => out.push((false, format!("{what} finished"), short(s(&j, "label")))),
                "failed" => {
                    let detail = if s(&j, "error").is_empty() { s(&j, "label") } else { s(&j, "error") };
                    out.push((true, format!("{what} failed"), short(detail)));
                }
                _ => {}
            }
        }
        seen.jobs.insert(id, st);
    }
    // Backup engine alerts (backup failed, another app took over, KopiaLens's server stopped).
    for a in state.get("alerts").and_then(Value::as_array).cloned().unwrap_or_default() {
        let id = a.get("id").and_then(Value::as_i64).unwrap_or(0);
        if id <= seen.alert {
            continue;
        }
        if seen.ready {
            out.push((s(&a, "level") != "info", s(&a, "title").to_string(), short(s(&a, "body"))));
        }
        seen.alert = id;
    }
    seen.ready = true;
    drop(seen);
    for (warning, title, body) in out {
        if level == "all" || (level == "warnings" && warning) {
            toast(app, &title, &body);
        }
    }
}

pub fn status_from(result: &Result<Value, ApiError>) -> Health {
    match result {
        Ok(v) => health(v),
        Err(ApiError::Unauthorized) | Err(ApiError::Failed(_)) => Health::Limited,
        Err(ApiError::Down) => Health::Down,
    }
}

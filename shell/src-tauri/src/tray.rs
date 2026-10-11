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

/// The original KopiaLens mark (a "Q" on a light tile), kept until the real logo exists.
fn icon() -> Image<'static> {
    const N: usize = 64;
    let (bg, teal) = ([250u8, 250, 249], [15u8, 118, 110]);
    let mut px = vec![0u8; N * N * 4];
    for y in 0..N {
        for x in 0..N {
            let (mut r, mut g, mut b) = (0u32, 0u32, 0u32);
            for sy in 0..4 {
                for sx in 0..4 {
                    let fx = x as f32 + (sx as f32 + 0.5) / 4.0;
                    let fy = y as f32 + (sy as f32 + 0.5) / 4.0;
                    let d = ((fx - 32.0).powi(2) + (fy - 32.0).powi(2)).sqrt();
                    let ring = d <= 24.0 && d >= 10.0;
                    let tail = (42.0..=58.0).contains(&fx) && (42.0..=50.0).contains(&fy);
                    let c = if ring || tail { teal } else { bg };
                    r += c[0] as u32;
                    g += c[1] as u32;
                    b += c[2] as u32;
                }
            }
            let i = (y * N + x) * 4;
            px[i..i + 4].copy_from_slice(&[(r / 16) as u8, (g / 16) as u8, (b / 16) as u8, 255]);
        }
    }
    Image::new_owned(px, N as u32, N as u32)
}

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
                                sub.append(&MenuItem::with_id(app, format!("repo:bpause:{key}"), "Pause backups until I resume", true, None::<&str>)?)?;
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
                    for (id, label) in [("pause:4", "For 4 hours"), ("pause:8", "For 8 hours"), ("pause:12", "For 12 hours"), ("pause:0", "Until I resume")] {
                        sub.append(&MenuItem::with_id(app, id, label, true, None::<&str>)?)?;
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
                    for (id, label) in [("bpause:1", "For 1 hour"), ("bpause:4", "For 4 hours"), ("bpause:24", "For 24 hours"), ("bpause:0", "Until I resume")] {
                        sub.append(&MenuItem::with_id(app, id, label, true, None::<&str>)?)?;
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
    let autostart = app.autolaunch().is_enabled().unwrap_or(false);
    menu.append(&CheckMenuItem::with_id(app, "autostart", "Start at logon", true, autostart, None::<&str>)?)?;
    menu.append(&sep()?)?;
    menu.append(&MenuItem::with_id(app, "data", "Open data folder", true, None::<&str>)?)?;
    menu.append(&MenuItem::with_id(app, "logs", "Open logs folder", true, None::<&str>)?)?;
    menu.append(&sep()?)?;
    if let Some(v) = state.map(|st| s(st, "version")).filter(|v| !v.is_empty()) {
        menu.append(&MenuItem::with_id(app, "status:version", format!("KopiaLens {v}"), false, None::<&str>)?)?;
    }
    if let Some(latest) = state.and_then(|st| st.get("update")).and_then(|u| u.get("latest")).and_then(Value::as_str) {
        menu.append(&MenuItem::with_id(app, "update", format!("Update available: {latest}"), true, None::<&str>)?)?;
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
    shell.server.log(&format!("start at logon: {:?}", app.autolaunch().is_enabled()));
    let menu = build_menu(app, h, state.as_ref())?;
    drop(app.remove_tray_by_id(TRAY_ID));
    TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon())
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
    let autostart = app.autolaunch().is_enabled().unwrap_or(false);
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

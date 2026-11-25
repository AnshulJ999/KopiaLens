// In-app updates (Tauri's updater): wait until no index or job is running, download and verify the
// signed update, then install it. On Windows the installer restarts KopiaLens; on macOS and the
// Linux AppImage the app restarts itself. Progress goes to the server, which shows it in the app.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};
use tauri::AppHandle;
use tauri_plugin_updater::UpdaterExt;

use crate::server::Layout;
use crate::{tray, Shell};

static RUNNING: AtomicBool = AtomicBool::new(false);

/// An installed copy (not portable, not a source checkout) built with the update key, in a form
/// the updater can replace: the Windows install, the macOS app, or the Linux AppImage (deb and
/// rpm installs are updated by installing the new package).
pub fn available(app: &AppHandle, sh: &Shell) -> bool {
    let key = app.config().plugins.0.get("updater")
        .and_then(|u| u.get("pubkey")).and_then(Value::as_str).unwrap_or("");
    let replaceable = cfg!(any(windows, target_os = "macos"))
        || std::env::var_os("APPIMAGE").is_some_and(|v| !v.is_empty());
    replaceable && !key.is_empty() && sh.server.portable.is_none()
        && matches!(sh.server.layout, Layout::Installed { .. })
}

fn report(sh: &Shell, state: &str, pct: Option<u64>, error: Option<&str>) {
    let _ = sh.server.post("/api/shell/update-progress", json!({"state": state, "pct": pct, "error": error}));
}

fn busy(sh: &Shell) -> bool {
    let Some(st) = sh.state.lock().unwrap().clone() else { return false };
    let any = |key: &str, f: &dyn Fn(&Value) -> bool| {
        st.get(key).and_then(Value::as_array).is_some_and(|items| items.iter().any(f))
    };
    any("repos", &|r| r.get("running").and_then(Value::as_bool).unwrap_or(false))
        || any("jobs", &|j| j.get("state").and_then(Value::as_str) == Some("running"))
}

pub fn install(app: &AppHandle) {
    if RUNNING.swap(true, Ordering::SeqCst) {
        return;
    }
    let app = app.clone();
    thread::spawn(move || {
        let sh = crate::shell(&app);
        if let Err(e) = run(&app, &sh) {
            sh.server.log(&format!("update failed: {e}"));
            report(&sh, "failed", None, Some(&e));
            tray::toast(&app, "Couldn't install the update", &e);
        }
        RUNNING.store(false, Ordering::SeqCst);
    });
}

fn wait_idle(app: &AppHandle, sh: &Shell) {
    if busy(sh) {
        report(sh, "waiting", None, None);
        tray::toast(app, "Update waiting", "KopiaLens installs it when indexing and running jobs finish.");
        while busy(sh) {
            thread::sleep(Duration::from_secs(5));
        }
    }
}

/// On the beta channel, the configured endpoints with `channel=beta` added to the update service's
/// address (GitHub's latest.json is stable only). None on the stable channel: the config is used as is.
fn channel_endpoints(app: &AppHandle, sh: &Shell) -> Option<Vec<tauri::Url>> {
    let beta = sh.state.lock().unwrap().as_ref()
        .and_then(|st| st.get("update")).and_then(|u| u.get("channel")).and_then(Value::as_str) == Some("beta");
    if !beta {
        return None;
    }
    let list = app.config().plugins.0.get("updater").and_then(|u| u.get("endpoints")).and_then(Value::as_array)?;
    let urls: Vec<tauri::Url> = list.iter().filter_map(Value::as_str).filter_map(|s| {
        let mut url = tauri::Url::parse(s).ok()?;
        if url.path().contains("/v1/update/") {
            url.query_pairs_mut().append_pair("channel", "beta");
        }
        Some(url)
    }).collect();
    (!urls.is_empty()).then_some(urls)
}

fn run(app: &AppHandle, sh: &Arc<Shell>) -> Result<(), String> {
    wait_idle(app, sh);
    report(sh, "checking", None, None);
    let stopper = sh.clone();
    // Windows quits the app to run the installer: stop indexing (it saves and resumes) and the server first.
    let mut builder = app.updater_builder()
        .on_before_exit(move || {
            stopper.server.log("update: stopping indexing and the server for the installer");
            stopper.server.stop_workers();
            stopper.server.stop();
        });
    if let Some(urls) = channel_endpoints(app, sh) {
        builder = builder.endpoints(urls).map_err(|e| e.to_string())?;
    }
    let updater = builder.build().map_err(|e| e.to_string())?;
    let Some(update) = tauri::async_runtime::block_on(updater.check()).map_err(|e| e.to_string())? else {
        report(sh, "none", None, None);
        return Ok(());
    };
    sh.server.log(&format!("update: downloading {}", update.version));
    report(sh, "downloading", Some(0), None);
    let (mut got, mut shown) = (0u64, 0u64);
    let bytes = tauri::async_runtime::block_on(update.download(
        |chunk, total| {
            got += chunk as u64;
            if let Some(t) = total.filter(|t| *t > 0) {
                let pct = got * 100 / t;
                if pct >= shown + 5 {
                    shown = pct;
                    report(sh, "downloading", Some(pct), None);
                }
            }
        },
        || {},
    )).map_err(|e| e.to_string())?;
    wait_idle(app, sh);   // a job started during the download
    report(sh, "installing", None, None);
    sh.server.log(&format!("update: starting the {} installer", update.version));
    update.install(bytes).map_err(|e| e.to_string())?;
    if !cfg!(windows) {
        // macOS and the AppImage replace the app in place; start the new version ourselves.
        sh.server.log(&format!("update: {} installed, restarting", update.version));
        crate::instance::exiting();
        sh.server.stop_workers();
        sh.server.stop();
        app.restart();
    }
    Ok(())
}

// Local control port for scripts and agents: POST /restart, POST /quit, GET /status. Loopback only,
// and never for a web page: a request with an Origin header, or a Host other than this computer, is
// refused. Restart and quit live here, not in the server: a process cannot stop itself and still answer.
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;

use serde_json::{json, Value};
use tauri::AppHandle;
use tiny_http::{Header, Method, Response, Server as HttpServer};

use crate::server::port_open;
use crate::Shell;

static REFUSED: AtomicU64 = AtomicU64::new(0);

fn reply(req: tiny_http::Request, code: u16, body: Value) {
    let header = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).expect("static header");
    let _ = req.respond(Response::from_string(body.to_string()).with_status_code(code).with_header(header));
}

/// A browser always sends Origin on a cross-site request and names the site it thinks it is talking
/// to in Host, so this refuses web pages (including DNS rebinding) while curl and scripts pass.
fn from_script(req: &tiny_http::Request) -> bool {
    let mut host_ok = false;
    for h in req.headers() {
        let name = h.field.as_str().as_str().to_ascii_lowercase();
        if name == "origin" {
            return false;
        }
        if name == "host" {
            let v = h.value.as_str().to_ascii_lowercase();
            let bare = v.rsplit_once(':').map_or(v.as_str(), |(h, _)| h);
            host_ok = bare == "127.0.0.1" || bare == "localhost" || bare == "[::1]";
        }
    }
    host_ok
}

/// On or off: `KOPIALENS_CONTROL_API` (1/0) wins, then Settings > Advanced, then the build:
/// on for the dev identity, off for a released copy.
fn enabled(app: &AppHandle, shell: &Shell) -> bool {
    if let Ok(v) = std::env::var("KOPIALENS_CONTROL_API") {
        return v.trim() != "0";
    }
    match shell.server.setting("control_api").and_then(|v| v.as_bool()) {
        Some(on) => on,
        None => app.config().identifier.ends_with(".dev"),
    }
}

/// Opens the control port before the server starts, so the server is told the one in use. When
/// another program (another KopiaLens copy) holds it, or it is the app's own port, the next free
/// port is used and saved in this copy's own settings.
pub fn bind(app: &AppHandle, shell: &Shell) -> Option<HttpServer> {
    let srv = &shell.server;
    if !enabled(app, shell) {
        srv.log("control port is off (Settings > Advanced)");
        return None;
    }
    let port = srv.control_port();
    let app_port = srv.port();
    let why = if port == app_port { "is the app's port" } else { "is used by another program" };
    if port != app_port {
        match HttpServer::http(("127.0.0.1", port)) {
            Ok(s) => return Some(s),
            Err(e) if !port_open(port) => {
                srv.log(&format!("control port {port} failed to bind: {e}"));
                return None;
            }
            Err(_) => {}
        }
    }
    let Some((next, http)) = (port.saturating_add(1)..port.saturating_add(100))
        .filter(|p| *p != app_port && !port_open(*p))
        .find_map(|p| HttpServer::http(("127.0.0.1", p)).ok().map(|s| (p, s)))
    else {
        srv.log(&format!("control port {port} {why} and no nearby port is free"));
        return None;
    };
    srv.log(&format!("control port {port} {why} - using {next}"));
    srv.set_control_port(next);
    if std::env::var_os("KOPIALENS_TRAY_PORT").is_none() {
        if let Err(e) = srv.save_setting("control_port", json!(next)) {
            srv.log(&format!("could not save control port {next}: {e}"));
        }
    }
    Some(http)
}

pub fn spawn(app: AppHandle, shell: Arc<Shell>, http: Option<HttpServer>) {
    let Some(http) = http else { return };
    thread::spawn(move || {
        for req in http.incoming_requests() {
            if !from_script(&req) {
                let n = REFUSED.fetch_add(1, Ordering::Relaxed) + 1;
                if n.is_power_of_two() {
                    shell.server.log(&format!("control port: refused a request from a web page ({n} so far)"));
                }
                reply(req, 403, json!({"ok": false, "detail": "not allowed from a web page"}));
                continue;
            }
            match (req.method(), req.url()) {
                (Method::Get, "/status") => {
                    let srv = &shell.server;
                    let body = json!({
                        "server_up": crate::server::port_open(srv.port()),
                        "pid": srv.server_pid(),
                        "url": srv.url(),
                        "indexing": srv.workers(),
                    });
                    reply(req, 200, body);
                }
                (Method::Post, "/restart") => {
                    shell.server.log("control port: restart");
                    let up = shell.server.restart();
                    reply(req, 200, json!({"ok": up, "server_up": up}));
                }
                (Method::Post, "/quit") => {
                    shell.server.log("control port: quit");
                    reply(req, 200, json!({"ok": true}));
                    crate::quit(&app, false, false);
                }
                _ => reply(req, 404, json!({"ok": false, "detail": "not found"})),
            }
        }
    });
}

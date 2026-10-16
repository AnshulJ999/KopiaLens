// Local control port for scripts: POST /restart, POST /quit, GET /status. Loopback only.
// Restart and quit live here, not in the server: a process cannot stop itself and still answer.
use std::sync::Arc;
use std::thread;

use serde_json::{json, Value};
use tauri::AppHandle;
use tiny_http::{Header, Method, Response, Server as HttpServer};

use crate::server::port_open;
use crate::Shell;

fn reply(req: tiny_http::Request, code: u16, body: Value) {
    let header = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).expect("static header");
    let _ = req.respond(Response::from_string(body.to_string()).with_status_code(code).with_header(header));
}

/// Opens the control port before the server starts, so the server is told the one in use. When
/// another program (another KopiaLens copy) holds it, or it is the app's own port, the next free
/// port is used and saved in this copy's own settings.
pub fn bind(shell: &Shell) -> Option<HttpServer> {
    let srv = &shell.server;
    if srv.setting("control_api").and_then(|v| v.as_bool()) == Some(false) {
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

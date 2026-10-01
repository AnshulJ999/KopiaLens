// Local control port for scripts: POST /restart, POST /quit, GET /status. Loopback only.
// Restart and quit live here, not in the server: a process cannot stop itself and still answer.
use std::sync::Arc;
use std::thread;

use serde_json::{json, Value};
use tauri::AppHandle;
use tiny_http::{Header, Method, Response, Server as HttpServer};

use crate::Shell;

fn reply(req: tiny_http::Request, code: u16, body: Value) {
    let header = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).expect("static header");
    let _ = req.respond(Response::from_string(body.to_string()).with_status_code(code).with_header(header));
}

pub fn spawn(app: AppHandle, shell: Arc<Shell>) {
    if shell.server.setting("control_api").and_then(|v| v.as_bool()) == Some(false) {
        shell.server.log("control port is off (Settings > Advanced)");
        return;
    }
    let port = shell.server.control_port;
    let http = match HttpServer::http(("127.0.0.1", port)) {
        Ok(s) => s,
        Err(e) => {
            shell.server.log(&format!("control port {port} failed to bind: {e}"));
            return;
        }
    };
    thread::spawn(move || {
        for req in http.incoming_requests() {
            match (req.method(), req.url()) {
                (Method::Get, "/status") => {
                    let srv = &shell.server;
                    let body = json!({
                        "server_up": crate::server::port_open(srv.port),
                        "pid": srv.child_pid(),
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

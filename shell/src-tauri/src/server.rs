// Starting, stopping and talking to the KopiaLens server.
//
// Two layouts: an install or portable copy ships the compiled server in `server/` beside this
// program (Tauri's resource folder) and runs it with `--server`; a source checkout runs
// `python main.py` from the KopiaLens folder.
use std::fs::OpenOptions;
use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System, UpdateKind};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub enum Layout {
    Installed { exe: PathBuf },
    Source { root: PathBuf, python: String },
}

pub struct Server {
    pub layout: Layout,
    /// The portable data folder when the portable marker is present.
    pub portable: Option<PathBuf>,
    pub root: PathBuf,
    pub data_dir: PathBuf,
    pub settings_file: PathBuf,
    port: AtomicU16,
    control_port: AtomicU16,
    /// A data folder the server must be told about (portable, or a source checkout's `.env.dev`).
    data_env: Option<PathBuf>,
    token: String,
    child: Mutex<Option<Child>>,
    agent: ureq::Agent,
}

/// Who is listening on the app's port.
pub enum PortOwner {
    Free,
    Ours,
    Other,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

fn env_port(name: &str, default: u16) -> u16 {
    env(name).and_then(|v| v.parse().ok()).unwrap_or(default)
}

/// A port saved in settings.json (Settings > Advanced). Mirrors config._setting_port.
fn setting_port(settings_file: &Path, key: &str, default: u16) -> u16 {
    std::fs::read_to_string(settings_file).ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.get("kopialens")?.get(key)?.as_u64())
        .filter(|p| (1..=65535).contains(p))
        .map(|p| p as u16)
        .unwrap_or(default)
}

/// A setting from a source checkout's git-ignored `.env.dev` (mirrors config._dev_env).
fn dev_env(root: &Path, name: &str) -> Option<String> {
    let text = std::fs::read_to_string(root.join(".env.dev")).ok()?;
    text.lines().find_map(|line| {
        let (key, value) = line.split_once('=')?;
        let value = value.trim().trim_matches('"');
        (key.trim() == name && !value.is_empty()).then(|| value.to_string())
    })
}

fn data_line(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    text.trim_start_matches('\u{feff}').lines().find_map(|line| {
        let (key, value) = line.split_once('=')?;
        let value = value.trim().trim_matches('"');
        (key.trim().eq_ignore_ascii_case("data") && !value.is_empty()).then(|| value.to_string())
    })
}

/// The portable data folder: `data/` beside the app, or a `data=<path>` line (relative paths are
/// beside the app) in the data file, else in the marker (older copies). Mirrors
/// config.portable_dir_of. A marker line is copied into the data file, which the portable zip
/// never contains, so a later update can't erase it.
fn portable_dir(app_dir: &Path) -> Option<PathBuf> {
    let marker = app_dir.join(PORTABLE_MARKER);
    if !marker.is_file() {
        return None;
    }
    let data_file = app_dir.join(PORTABLE_DATA_FILE);
    let custom = data_line(&data_file).or_else(|| {
        let value = data_line(&marker)?;
        if !data_file.exists() {
            let _ = std::fs::write(&data_file, format!("data={value}\r\n"));
        }
        Some(value)
    }).map(PathBuf::from);
    Some(match custom {
        Some(p) if p.is_absolute() => p,
        Some(p) => app_dir.join(p),
        None => app_dir.join("data"),
    })
}

fn server_exe_name() -> &'static str {
    if cfg!(windows) { "kopialens-server.exe" } else { "kopialens-server" }
}

/// Same names as config.PORTABLE_MARKER and config.PORTABLE_DATA_FILE.
const PORTABLE_MARKER: &str = "KopiaLens-portable.txt";
const PORTABLE_DATA_FILE: &str = "KopiaLens-data.txt";

fn installed_in(dir: &Path) -> Option<(Layout, PathBuf)> {
    let root = dir.join("server");
    let exe = root.join(server_exe_name());
    exe.is_file().then(|| (Layout::Installed { exe }, root))
}

fn find_layout(resource_dir: Option<PathBuf>) -> Option<(Layout, PathBuf)> {
    if let Some(home) = env("KOPIALENS_HOME") {
        let root = PathBuf::from(home);
        if let Some(found) = installed_in(&root) {
            return Some(found);
        }
        return Some((Layout::Source { root: root.clone(), python: python() }, root));
    }
    let me = std::env::current_exe().ok()?;
    let dir = me.parent()?.to_path_buf();
    for base in resource_dir.iter().chain(std::iter::once(&dir)) {
        if let Some(found) = installed_in(base) {
            return Some(found);
        }
    }
    // Source checkout: the shell is built inside KopiaLens/shell/, so walk up to main.py.
    for anc in dir.ancestors() {
        if anc.join("main.py").is_file() && anc.join("config.py").is_file() {
            let root = anc.to_path_buf();
            return Some((Layout::Source { root: root.clone(), python: python() }, root));
        }
    }
    None
}

fn python() -> String {
    env("KOPIALENS_PYTHON").unwrap_or_else(|| if cfg!(windows) { "pythonw".into() } else { "python3".into() })
}

// Mirrors config.py: the portable folder, else KOPIALENS_DATA_DIR, else a source checkout's
// `.env.dev`, else the old layout (repos.json next to the code), else the per-user data folder.
fn data_dirs(root: &Path, portable: Option<&PathBuf>, dev: Option<&PathBuf>) -> (PathBuf, PathBuf) {
    let chosen = portable.cloned()
        .or_else(|| env("KOPIALENS_DATA_DIR").map(PathBuf::from))
        .or_else(|| dev.cloned());
    if let Some(d) = chosen {
        let cfg = env("KOPIALENS_CONFIG_DIR").map(PathBuf::from).unwrap_or_else(|| d.clone());
        return (d, cfg);
    }
    let (data, cfg) = if root.join("repos.json").is_file() {
        (root.join("data"), root.to_path_buf())
    } else {
        let d = user_data_root();
        (d.clone(), d)
    };
    (data, env("KOPIALENS_CONFIG_DIR").map(PathBuf::from).unwrap_or(cfg))
}

fn user_data_root() -> PathBuf {
    let home = env("USERPROFILE").or_else(|| env("HOME")).map(PathBuf::from).unwrap_or_default();
    if cfg!(windows) {
        env("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| home.join("AppData").join("Local")).join("KopiaLens")
    } else if cfg!(target_os = "macos") {
        home.join("Library").join("Application Support").join("KopiaLens")
    } else {
        env("XDG_DATA_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".local").join("share")).join("kopialens")
    }
}

pub fn new_token() -> String {
    let mut buf = [0u8; 24];
    getrandom::fill(&mut buf).expect("OS random source");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(400)).is_ok()
}

fn wait_for(port: u16, up: bool, timeout: Duration) -> bool {
    let end = Instant::now() + timeout;
    while Instant::now() < end {
        if port_open(port) == up {
            return true;
        }
        thread::sleep(Duration::from_millis(300));
    }
    false
}

pub enum ApiError {
    Down,
    Unauthorized,
    Failed(String),
}

impl Server {
    pub fn locate(resource_dir: Option<PathBuf>) -> Option<Server> {
        let (layout, root) = find_layout(resource_dir)?;
        let app_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
        let portable = portable_dir(&app_dir);
        let dev = match &layout {
            Layout::Source { root, .. } => dev_env(root, "KOPIALENS_DATA_DIR").map(PathBuf::from),
            Layout::Installed { .. } => None,
        };
        let (data_dir, cfg_dir) = data_dirs(&root, portable.as_ref(), dev.as_ref());
        let data_env = portable.clone().or(if env("KOPIALENS_DATA_DIR").is_none() { dev } else { None });
        let settings_file = cfg_dir.join("settings.json");
        Some(Server {
            layout,
            portable,
            root,
            port: AtomicU16::new(env_port("KOPIALENS_PORT", setting_port(&settings_file, "port", 7123))),
            control_port: AtomicU16::new(env_port("KOPIALENS_TRAY_PORT", setting_port(&settings_file, "control_port", 7124))),
            settings_file,
            data_dir,
            data_env,
            token: new_token(),
            child: Mutex::new(None),
            agent: ureq::AgentBuilder::new().timeout(Duration::from_secs(5)).build(),
        })
    }

    pub fn port(&self) -> u16 {
        self.port.load(Ordering::SeqCst)
    }

    /// A new port for this run (the user chose another one because the saved port was taken).
    pub fn set_port(&self, port: u16) {
        self.port.store(port, Ordering::SeqCst);
    }

    pub fn control_port(&self) -> u16 {
        self.control_port.load(Ordering::SeqCst)
    }

    /// The control port this run actually listens on (another copy held the saved one).
    pub fn set_control_port(&self, port: u16) {
        self.control_port.store(port, Ordering::SeqCst);
    }

    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port())
    }

    pub fn log(&self, msg: &str) {
        let dir = self.data_dir.join("logs");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("shell.log");
        if std::fs::metadata(&path).map(|m| m.len() > 5 * 1024 * 1024).unwrap_or(false) {
            let _ = std::fs::rename(&path, dir.join("shell.log.1"));
        }
        let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
            let _ = writeln!(f, "{secs} {msg}");
        }
    }

    /// One `kopialens` setting read straight from the file (used before the server answers).
    pub fn setting(&self, key: &str) -> Option<Value> {
        let text = std::fs::read_to_string(&self.settings_file).ok()?;
        let v: Value = serde_json::from_str(&text).ok()?;
        v.get("kopialens")?.get(key).cloned()
    }

    pub fn child_pid(&self) -> Option<u32> {
        let mut guard = self.child.lock().unwrap();
        let c = guard.as_mut()?;
        matches!(c.try_wait(), Ok(None)).then(|| c.id())
    }

    /// The running server's pid: the one this app started, else this data folder's own server
    /// left by an earlier launch.
    pub fn server_pid(&self) -> Option<u32> {
        self.child_pid().or_else(|| match self.port_owner() {
            PortOwner::Ours => self.recorded().map(|(pid, _)| pid),
            _ => None,
        })
    }

    pub fn start(&self) {
        if self.child_pid().is_some() {
            return;
        }
        if port_open(self.port()) {
            self.log(&format!("server already listening on port {} - not starting another", self.port()));
            return;
        }
        let mut cmd = match &self.layout {
            Layout::Installed { exe } => {
                let mut c = Command::new(exe);
                c.arg("--server");
                c
            }
            Layout::Source { root, python } => {
                let mut c = Command::new(python);
                c.arg(root.join("main.py"));
                c
            }
        };
        if let Some(d) = &self.data_env {
            cmd.env("KOPIALENS_DATA_DIR", d);
        }
        cmd.current_dir(&self.root)
            .env("KOPIALENS_PORT", self.port().to_string())
            .env("KOPIALENS_TRAY_PORT", self.control_port().to_string())
            .env("KOPIALENS_PROFILE", "desktop")
            .env("KOPIALENS_SHELL_TOKEN", &self.token)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);
        match cmd.spawn() {
            Ok(child) => {
                self.log(&format!("started server pid {}", child.id()));
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => self.log(&format!("could not start the server: {e}")),
        }
    }

    /// Whether the app's port is free, held by this data folder's server (left running by an
    /// earlier launch), or held by another program or another KopiaLens. The server records its
    /// pid and port in `<data>/server.json`; the pid must still be a KopiaLens server of this layout.
    pub fn port_owner(&self) -> PortOwner {
        if !port_open(self.port()) {
            return PortOwner::Free;
        }
        if let Some((pid, port)) = self.recorded() {
            let sys = Self::processes();
            if port == self.port() && sys.process(Pid::from_u32(pid)).is_some_and(|p| self.is_server_cmd(&Self::cmdline(p))) {
                return PortOwner::Ours;
            }
        }
        PortOwner::Other
    }

    /// The pid and port this data folder's server recorded in `<data>/server.json`.
    fn recorded(&self) -> Option<(u32, u16)> {
        let v: Value = serde_json::from_str(&std::fs::read_to_string(self.data_dir.join("server.json")).ok()?).ok()?;
        Some((v.get("pid")?.as_u64()? as u32, v.get("port")?.as_u64()? as u16))
    }

    /// The first free port after the app's port, for "use another port".
    pub fn next_free_port(&self) -> Option<u16> {
        (self.port().saturating_add(1)..self.port().saturating_add(100))
            .find(|p| *p != self.control_port() && !port_open(*p))
    }

    /// Saves the app's port in settings.json (only before the server starts, so nothing else
    /// writes the file at the same time).
    pub fn save_port(&self, port: u16) -> std::io::Result<()> {
        self.save_setting("port", json!(port))
    }

    /// One `kopialens` setting written to settings.json, under the same before-the-server rule.
    pub fn save_setting(&self, key: &str, value: Value) -> std::io::Result<()> {
        let mut doc: Value = std::fs::read_to_string(&self.settings_file).ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({}));
        if !doc.get("kopialens").is_some_and(Value::is_object) {
            doc["kopialens"] = json!({});
        }
        doc["kopialens"][key] = value;
        if let Some(dir) = self.settings_file.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = self.settings_file.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(&doc).unwrap_or_default())?;
        std::fs::rename(&tmp, &self.settings_file)
    }

    /// The server's own command line, to find one this shell did not start. Matched by command
    /// line, never by PID alone (PIDs get recycled).
    fn is_server_cmd(&self, cmd: &str) -> bool {
        match &self.layout {
            Layout::Installed { exe } => {
                cmd.contains(&exe.to_string_lossy().to_lowercase()) && cmd.contains("--server")
            }
            Layout::Source { root, .. } => cmd.contains(&root.join("main.py").to_string_lossy().to_lowercase()),
        }
    }

    fn processes() -> System {
        let mut sys = System::new();
        sys.refresh_processes_specifics(
            ProcessesToUpdate::All,
            true,
            ProcessRefreshKind::nothing().with_cmd(UpdateKind::Always),
        );
        sys
    }

    fn cmdline(p: &sysinfo::Process) -> String {
        p.cmd().iter().map(|a| a.to_string_lossy()).collect::<Vec<_>>().join(" ").to_lowercase()
    }

    pub fn stop(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            if matches!(child.try_wait(), Ok(None)) {
                let _ = child.kill();
                let _ = child.wait();
                self.log("stopped server");
                return;
            }
        }
        // A server this app did not start (left by an earlier launch): only the one this data
        // folder recorded, never another copy's server from the same program folder.
        let Some((pid, _)) = self.recorded() else { return };
        let sys = Self::processes();
        if let Some(p) = sys.process(Pid::from_u32(pid))
            .filter(|p| pid != std::process::id() && self.is_server_cmd(&Self::cmdline(p)))
        {
            self.log(&format!("stopping a server this app did not start (pid {pid})"));
            p.kill();
        }
    }

    /// True once the NEW server accepts connections. If the old one never lets go of the port,
    /// starting would be refused and the old process would answer the port check.
    pub fn restart(&self) -> bool {
        self.stop();
        if !wait_for(self.port(), false, Duration::from_secs(15)) {
            self.log("the old server never released the port - restart aborted");
            return false;
        }
        self.start();
        wait_for(self.port(), true, Duration::from_secs(60))
    }

    pub fn wait_up(&self, timeout: Duration) -> bool {
        wait_for(self.port(), true, timeout)
    }

    /// Index workers that look alive, from their own progress files (works with the server down).
    pub fn workers(&self) -> Vec<Value> {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        let Ok(entries) = std::fs::read_dir(&self.data_dir) else { return vec![] };
        let mut out = vec![];
        for e in entries.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if !(name.starts_with("index_progress_repo") && name.ends_with(".json")) {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(e.path()) else { continue };
            let Ok(info) = serde_json::from_str::<Value>(&text) else { continue };
            let updated = info.get("updated").and_then(Value::as_f64).unwrap_or(now);
            out.push(json!({
                "repo": info.get("repo"), "stage": info.get("stage"), "pid": info.get("pid"),
                "started": info.get("started"), "indexed": info.get("indexed"),
                "total": info.get("total"), "age": ((now - updated) * 10.0).round() / 10.0,
            }));
        }
        out
    }

    /// Ends running index workers on quit, confirmed by command line. A worker reading folders is
    /// first asked to save and stop (the same flag the app's Stop writes) and gets up to a minute;
    /// anything still running after that is killed. The next start records the run as interrupted.
    pub fn stop_workers(&self) {
        const GRACE: Duration = Duration::from_secs(60);
        let sys = Self::processes();
        let mut asked: Vec<u32> = vec![];
        for w in self.workers() {
            let Some(pid) = w.get("pid").and_then(Value::as_u64) else { continue };
            let Some(p) = sys.process(Pid::from_u32(pid as u32)) else { continue };
            let cmd = Self::cmdline(p);
            let reading = cmd.contains("app.index.worker")
                && w.get("stage").and_then(Value::as_str) == Some("index");
            let repo = match w.get("repo") {
                Some(Value::String(s)) => Some(s.clone()),
                Some(Value::Number(n)) => Some(n.to_string()),
                _ => None,
            };
            if let (true, Some(repo)) = (reading, repo) {
                let flag = self.data_dir.join(format!("index_stop_repo{repo}.flag"));
                let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
                if std::fs::write(&flag, now.to_string()).is_ok() {
                    self.log(&format!("asking the index worker pid {pid} to save and stop on quit"));
                    asked.push(pid as u32);
                    continue;
                }
            }
            if cmd.contains("app.index.worker") || cmd.contains("app.backblaze.worker") {
                self.log(&format!("stopping the index worker pid {pid} on quit"));
                p.kill();
            }
        }
        // A finished worker's pid can be reused, so "still running" means still that worker.
        let still_worker = |sys: &System, pid: u32| {
            sys.process(Pid::from_u32(pid)).is_some_and(|p| Self::cmdline(p).contains("app.index.worker"))
        };
        let deadline = Instant::now() + GRACE;
        while !asked.is_empty() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(500));
            let sys = Self::processes();
            asked.retain(|pid| still_worker(&sys, *pid));
        }
        let sys = Self::processes();
        for pid in asked {
            if still_worker(&sys, pid) {
                if let Some(p) = sys.process(Pid::from_u32(pid)) {
                    self.log(&format!("index worker pid {pid} did not stop in time on quit - killing it"));
                    p.kill();
                }
            }
        }
    }

    fn call(&self, req: ureq::Request, body: Option<Value>) -> Result<Value, ApiError> {
        let req = req.set("X-KopiaLens-Shell", &self.token);
        let res = match body {
            Some(b) => req.send_json(b),
            None => req.call(),
        };
        match res {
            Ok(r) => r.into_json::<Value>().map_err(|e| ApiError::Failed(e.to_string())),
            Err(ureq::Error::Status(401, _)) | Err(ureq::Error::Status(403, _)) => Err(ApiError::Unauthorized),
            Err(ureq::Error::Status(_, r)) => {
                let detail = r.into_json::<Value>().ok()
                    .and_then(|v| v.get("detail").and_then(Value::as_str).map(str::to_string))
                    .unwrap_or_else(|| "the server refused the request".into());
                Err(ApiError::Failed(detail))
            }
            Err(_) => Err(ApiError::Down),
        }
    }

    pub fn get(&self, path: &str) -> Result<Value, ApiError> {
        self.call(self.agent.get(&format!("{}{path}", self.url())), None)
    }

    pub fn post(&self, path: &str, body: Value) -> Result<Value, ApiError> {
        self.call(self.agent.post(&format!("{}{path}", self.url())), Some(body))
    }
}

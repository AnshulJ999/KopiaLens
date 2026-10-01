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
    /// `data/` beside the app when the portable marker is present.
    pub portable: Option<PathBuf>,
    pub root: PathBuf,
    pub data_dir: PathBuf,
    pub settings_file: PathBuf,
    pub port: u16,
    pub control_port: u16,
    token: String,
    child: Mutex<Option<Child>>,
    agent: ureq::Agent,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

fn env_port(name: &str, default: u16) -> u16 {
    env(name).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn server_exe_name() -> &'static str {
    if cfg!(windows) { "kopialens-server.exe" } else { "kopialens-server" }
}

/// Same name as config.PORTABLE_MARKER.
const PORTABLE_MARKER: &str = "KopiaLens-portable.txt";

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

// Mirrors config.py: KOPIALENS_DATA_DIR, else the old layout (repos.json next to the code), else
// the per-user data folder.
fn data_dirs(root: &Path, portable: Option<&PathBuf>) -> (PathBuf, PathBuf) {
    if let Some(d) = env("KOPIALENS_DATA_DIR").map(PathBuf::from).or_else(|| portable.cloned()) {
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

fn new_token() -> String {
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
        let portable = app_dir.join(PORTABLE_MARKER).is_file().then(|| app_dir.join("data"));
        let (data_dir, cfg_dir) = data_dirs(&root, portable.as_ref());
        Some(Server {
            layout,
            portable,
            root,
            settings_file: cfg_dir.join("settings.json"),
            data_dir,
            port: env_port("KOPIALENS_PORT", 7123),
            control_port: env_port("KOPIALENS_TRAY_PORT", 7124),
            token: new_token(),
            child: Mutex::new(None),
            agent: ureq::AgentBuilder::new().timeout(Duration::from_secs(5)).build(),
        })
    }

    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
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

    pub fn start(&self) {
        if self.child_pid().is_some() {
            return;
        }
        if port_open(self.port) {
            self.log(&format!("server already listening on port {} - not starting another", self.port));
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
        if let (Some(p), None) = (&self.portable, env("KOPIALENS_DATA_DIR")) {
            cmd.env("KOPIALENS_DATA_DIR", p);
        }
        cmd.current_dir(&self.root)
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
        let sys = Self::processes();
        let me = std::process::id();
        for (pid, p) in sys.processes() {
            if pid.as_u32() != me && self.is_server_cmd(&Self::cmdline(p)) {
                self.log(&format!("stopping a server this app did not start (pid {pid})"));
                p.kill();
            }
        }
    }

    /// True once the NEW server accepts connections. If the old one never lets go of the port,
    /// starting would be refused and the old process would answer the port check.
    pub fn restart(&self) -> bool {
        self.stop();
        if !wait_for(self.port, false, Duration::from_secs(15)) {
            self.log("the old server never released the port - restart aborted");
            return false;
        }
        self.start();
        wait_for(self.port, true, Duration::from_secs(60))
    }

    pub fn wait_up(&self, timeout: Duration) -> bool {
        wait_for(self.port, true, timeout)
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

    /// Ends running index workers on quit, confirmed by command line. The next start records the
    /// run as interrupted and retries it.
    pub fn stop_workers(&self) {
        let sys = Self::processes();
        for w in self.workers() {
            let Some(pid) = w.get("pid").and_then(Value::as_u64) else { continue };
            if let Some(p) = sys.process(Pid::from_u32(pid as u32)) {
                let cmd = Self::cmdline(p);
                if cmd.contains("app.index.worker") || cmd.contains("app.backblaze.worker") {
                    self.log(&format!("stopping the index worker pid {pid} on quit"));
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

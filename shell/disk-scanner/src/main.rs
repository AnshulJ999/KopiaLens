//! kopialens-scan: answers KopiaLens's disk questions quickly on every platform.
//!
//!   kopialens-scan totals <root> [--depth N]
//!       size, file and folder counts for <root> and for every folder up to N levels below it
//!   kopialens-scan find <root> <pattern> [--or <pattern>]... [--dirs] [--fold] [--limit N]
//!       entries whose name matches a glob (`*`, `?`, `[...]`, `[!...]`, like Python's fnmatch)
//!
//! Shared options: `--time-ms N` (stop after N ms; unfinished folders report `complete: false`),
//! `--threads N`, `--skip <path>` (repeatable, a folder never entered), `--one-fs` (never enter
//! another filesystem; Linux/macOS), `--follow-links` (enter linked folders, each target once),
//! `--progress-ms N` (a `{"progress": true, ...}` line every N ms), `--watch-stdin` (exit when
//! stdin closes, so the scanner never outlives the program that started it).
//! Output: one JSON object per line, then `{"end": true, ...}`. Without `--follow-links`,
//! symlinks and junctions are never followed or counted; only regular files count.

use std::collections::HashSet;
use std::fs;
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// How many entries of one folder are read between two checks of the time limit.
const CHECK_EVERY: u64 = 2048;
/// Paths listed in the end line for unreadable folders and other filesystems.
const LIST_MAX: usize = 50;

#[derive(PartialEq)]
enum Mode {
    Totals,
    Find,
}

struct Opts {
    mode: Mode,
    root: PathBuf,
    depth: usize,
    time: Option<Duration>,
    threads: usize,
    skip: Vec<String>,
    patterns: Vec<Vec<char>>,
    dirs_only: bool,
    fold: bool,
    limit: usize,
    one_fs: bool,
    follow: bool,
    progress: Option<Duration>,
    watch_stdin: bool,
}

struct Task {
    path: PathBuf,
    depth: usize,
    chain: Vec<usize>,
}

#[derive(Default)]
struct Node {
    rel: Vec<String>,
    size: u64,
    files: u64,
    dirs: u64,
    cut: bool,
}

struct Queue {
    tasks: Vec<Task>,
    active: usize,
    done: bool,
}

struct Shared {
    queue: Mutex<Queue>,
    wake: Condvar,
    stop: AtomicBool,
    limited: AtomicBool,
    nodes: Mutex<Vec<Node>>,
    hits: Mutex<Vec<Value>>,
    errors: AtomicU64,
    unreadable: Mutex<Vec<String>>,
    other_fs: Mutex<Vec<String>>,
    links: Mutex<HashSet<PathBuf>>,
    seen_files: AtomicU64,
    seen_dirs: AtomicU64,
    seen_bytes: AtomicU64,
    root_dev: Option<u64>,
    deadline: Option<Instant>,
}

fn usage() -> ! {
    eprintln!("usage: kopialens-scan totals <root> [--depth N] | find <root> <pattern> [--or <pattern>]... [--dirs] [--fold] [--limit N]");
    eprintln!("       shared: [--time-ms N] [--threads N] [--skip <path>]... [--one-fs] [--follow-links] [--progress-ms N] [--watch-stdin]");
    std::process::exit(2);
}

fn number(v: Option<String>) -> u64 {
    v.and_then(|s| s.parse().ok()).unwrap_or_else(|| usage())
}

/// A path as compared with `--skip`: separators unified, no trailing separator, case folded on
/// Windows (where paths are case-insensitive).
fn path_key(p: &Path) -> String {
    let s = p.to_string_lossy().replace('\\', "/");
    let s = if s.len() > 1 { s.trim_end_matches('/').to_string() } else { s };
    if cfg!(windows) {
        s.to_lowercase()
    } else {
        s
    }
}

fn parse() -> Opts {
    let mut args = std::env::args().skip(1);
    let mode = match args.next().as_deref() {
        Some("totals") => Mode::Totals,
        Some("find") => Mode::Find,
        Some("--version") => {
            println!("kopialens-scan {}", env!("CARGO_PKG_VERSION"));
            std::process::exit(0);
        }
        _ => usage(),
    };
    let root = PathBuf::from(args.next().unwrap_or_else(|| usage()));
    let mut patterns = Vec::new();
    if mode == Mode::Find {
        patterns.push(args.next().unwrap_or_else(|| usage()));
    }
    let auto = thread::available_parallelism().map(|n| n.get()).unwrap_or(4).min(8);
    let mut o = Opts {
        mode,
        root,
        depth: 1,
        time: None,
        threads: auto,
        skip: Vec::new(),
        patterns: Vec::new(),
        dirs_only: false,
        fold: false,
        limit: usize::MAX,
        one_fs: false,
        follow: false,
        progress: None,
        watch_stdin: false,
    };
    while let Some(a) = args.next() {
        match a.as_str() {
            "--depth" => o.depth = number(args.next()) as usize,
            "--time-ms" => o.time = Some(Duration::from_millis(number(args.next()))),
            "--threads" => o.threads = (number(args.next()) as usize).clamp(1, 64),
            "--skip" => o.skip.push(path_key(Path::new(&args.next().unwrap_or_else(|| usage())))),
            "--or" => patterns.push(args.next().unwrap_or_else(|| usage())),
            "--dirs" => o.dirs_only = true,
            "--fold" => o.fold = true,
            "--limit" => o.limit = (number(args.next()) as usize).max(1),
            "--one-fs" => o.one_fs = true,
            "--follow-links" => o.follow = true,
            "--progress-ms" => o.progress = Some(Duration::from_millis(number(args.next()).max(100))),
            "--watch-stdin" => o.watch_stdin = true,
            _ => usage(),
        }
    }
    let fold = o.fold;
    o.patterns = patterns
        .into_iter()
        .map(|p| if fold { p.to_lowercase() } else { p }.chars().collect())
        .collect();
    o
}

/// One `[...]` class starting at `p[i] == '['`: (index after the class, does `c` match). None when
/// the class never closes, in which case the `[` is a literal (fnmatch's rule).
fn class(p: &[char], i: usize, c: char) -> Option<(usize, bool)> {
    let mut j = i + 1;
    let negate = j < p.len() && p[j] == '!';
    if negate {
        j += 1;
    }
    let start = j;
    let mut hit = false;
    while j < p.len() && (p[j] != ']' || j == start) {
        if j + 2 < p.len() && p[j + 1] == '-' && p[j + 2] != ']' {
            if p[j] <= c && c <= p[j + 2] {
                hit = true;
            }
            j += 3;
        } else {
            if p[j] == c {
                hit = true;
            }
            j += 1;
        }
    }
    if j >= p.len() {
        return None;
    }
    Some((j + 1, hit != negate))
}

fn glob(p: &[char], s: &[char]) -> bool {
    let (mut pi, mut si) = (0, 0);
    let mut star: Option<(usize, usize)> = None;
    while si < s.len() {
        let step = if pi < p.len() {
            match p[pi] {
                '*' => {
                    star = Some((pi, si));
                    pi += 1;
                    continue;
                }
                '?' => Some(pi + 1),
                '[' => match class(p, pi, s[si]) {
                    Some((next, true)) => Some(next),
                    Some((_, false)) => None,
                    None => (s[si] == '[').then_some(pi + 1),
                },
                c => (c == s[si]).then_some(pi + 1),
            }
        } else {
            None
        };
        match step {
            Some(next) => {
                pi = next;
                si += 1;
            }
            None => match star {
                Some((sp, ss)) => {
                    pi = sp + 1;
                    si = ss + 1;
                    star = Some((sp, ss + 1));
                }
                None => return false,
            },
        }
    }
    p[pi..].iter().all(|&c| c == '*')
}

fn matches(o: &Opts, name: &str) -> bool {
    let chars: Vec<char> = if o.fold { name.to_lowercase().chars().collect() } else { name.chars().collect() };
    o.patterns.iter().any(|p| glob(p, &chars))
}

fn add_hit(sh: &Shared, o: &Opts, hit: Value) {
    let mut hits = sh.hits.lock().unwrap();
    if hits.len() < o.limit {
        hits.push(hit);
    }
    if hits.len() >= o.limit {
        sh.limited.store(true, Ordering::Relaxed);
        sh.stop.store(true, Ordering::Relaxed);
    }
}

fn note(list: &Mutex<Vec<String>>, path: &Path) {
    let mut l = list.lock().unwrap();
    if l.len() < LIST_MAX {
        l.push(path.to_string_lossy().into_owned());
    }
}

#[cfg(unix)]
fn device(m: &fs::Metadata) -> Option<u64> {
    use std::os::unix::fs::MetadataExt;
    Some(m.dev())
}

#[cfg(not(unix))]
fn device(_m: &fs::Metadata) -> Option<u64> {
    None
}

/// Is a folder on another filesystem than the root (only asked with `--one-fs`)?
fn other_fs(sh: &Shared, m: &fs::Metadata) -> bool {
    matches!((sh.root_dev, device(m)), (Some(r), Some(d)) if r != d)
}

/// A link to follow: the folder or file it points at, or None when it is broken, already
/// followed once, or leads back into a folder above it (a loop).
fn link_target(sh: &Shared, path: &Path) -> Option<(fs::Metadata, bool)> {
    let meta = fs::metadata(path).ok()?;
    if !meta.is_dir() {
        return Some((meta, false));
    }
    let target = fs::canonicalize(path).ok()?;
    if let Some(parent) = path.parent().and_then(|p| fs::canonicalize(p).ok()) {
        if parent.starts_with(&target) {
            return None;
        }
    }
    if !sh.links.lock().unwrap().insert(target) {
        return None;
    }
    Some((meta, true))
}

fn time_up(sh: &Shared) -> bool {
    if sh.deadline.is_some_and(|d| Instant::now() >= d) {
        sh.stop.store(true, Ordering::Relaxed);
    }
    sh.stop.load(Ordering::Relaxed)
}

/// Reads one folder: adds its files to every folder in its chain and returns its subfolders.
fn visit(sh: &Shared, o: &Opts, t: &Task) -> Vec<Task> {
    let rd = match fs::read_dir(&t.path) {
        Ok(rd) => rd,
        Err(_) => {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            note(&sh.unreadable, &t.path);
            return Vec::new();
        }
    };
    sh.seen_dirs.fetch_add(1, Ordering::Relaxed);
    let (mut size, mut files, mut read) = (0u64, 0u64, 0u64);
    let mut cut = false;
    let mut subdirs: Vec<(PathBuf, String)> = Vec::new();
    for ent in rd {
        read += 1;
        if read % CHECK_EVERY == 0 && time_up(sh) {
            cut = true;
            break;
        }
        let Ok(ent) = ent else {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            continue;
        };
        let Ok(ft) = ent.file_type() else {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            continue;
        };
        let name = ent.file_name().to_string_lossy().into_owned();
        let path = ent.path();
        let (is_dir, is_file, linked_meta) = if ft.is_symlink() {
            if !o.follow {
                continue;
            }
            match link_target(sh, &path) {
                Some((m, dir)) => (dir, m.is_file(), Some(m)),
                None => continue,
            }
        } else {
            (ft.is_dir(), ft.is_file(), None)
        };
        if is_dir {
            if !o.skip.is_empty() && o.skip.contains(&path_key(&path)) {
                continue;
            }
            if o.one_fs {
                let m = match &linked_meta {
                    Some(m) => Some(m.clone()),
                    None => ent.metadata().ok(),
                };
                if m.is_some_and(|m| other_fs(sh, &m)) {
                    note(&sh.other_fs, &path);
                    continue;
                }
            }
            if o.mode == Mode::Find && matches(o, &name) {
                add_hit(sh, o, json!({"path": path.to_string_lossy(), "dir": true}));
            }
            subdirs.push((path, name));
        } else if is_file {
            let wanted = o.mode == Mode::Totals || (!o.dirs_only && matches(o, &name));
            if !wanted {
                continue;
            }
            let len = match &linked_meta {
                Some(m) => Some(m.len()),
                // On Windows the listing already carries the size; elsewhere this is one lstat.
                None => ent.metadata().map(|m| m.len()).ok(),
            };
            if o.mode == Mode::Totals {
                match len {
                    Some(n) => size += n,
                    None => {
                        sh.errors.fetch_add(1, Ordering::Relaxed);
                    }
                }
                files += 1;
                sh.seen_files.fetch_add(1, Ordering::Relaxed);
                sh.seen_bytes.fetch_add(len.unwrap_or(0), Ordering::Relaxed);
            } else {
                add_hit(sh, o, json!({"path": path.to_string_lossy(), "dir": false, "size": len}));
            }
        }
    }
    if o.mode == Mode::Find {
        if cut {
            return Vec::new();
        }
        return subdirs.into_iter().map(|(path, _)| Task { path, depth: t.depth + 1, chain: Vec::new() }).collect();
    }
    let mut nodes = sh.nodes.lock().unwrap();
    for &id in &t.chain {
        let n = &mut nodes[id];
        n.size += size;
        n.files += files;
        n.dirs += subdirs.len() as u64;
        n.cut |= cut;
    }
    if cut {
        return Vec::new();
    }
    let parent_rel = nodes[*t.chain.last().expect("totals chain")].rel.clone();
    subdirs
        .into_iter()
        .map(|(path, name)| {
            let mut chain = t.chain.clone();
            if t.depth < o.depth {
                let mut rel = parent_rel.clone();
                rel.push(name);
                nodes.push(Node { rel, ..Node::default() });
                chain.push(nodes.len() - 1);
            }
            Task { path, depth: t.depth + 1, chain }
        })
        .collect()
}

/// A folder that was never read: every folder above it is incomplete.
fn cut(sh: &Shared, t: &Task) {
    if t.chain.is_empty() {
        return;
    }
    let mut nodes = sh.nodes.lock().unwrap();
    for &id in &t.chain {
        nodes[id].cut = true;
    }
}

fn worker(sh: &Shared, o: &Opts) {
    loop {
        let task = {
            let mut q = sh.queue.lock().unwrap();
            loop {
                if q.done {
                    return;
                }
                if let Some(t) = q.tasks.pop() {
                    q.active += 1;
                    break t;
                }
                if q.active == 0 {
                    q.done = true;
                    sh.wake.notify_all();
                    return;
                }
                q = sh.wake.wait(q).unwrap();
            }
        };
        let next = if time_up(sh) {
            cut(sh, &task);
            Vec::new()
        } else {
            visit(sh, o, &task)
        };
        let mut q = sh.queue.lock().unwrap();
        q.active -= 1;
        let added = next.len();
        q.tasks.extend(next);
        if added > 1 {
            sh.wake.notify_all();
        } else if added == 1 {
            sh.wake.notify_one();
        } else if q.active == 0 && q.tasks.is_empty() {
            q.done = true;
            sh.wake.notify_all();
        }
    }
}

fn print_line(line: &Value) {
    let mut out = io::stdout().lock();
    let _ = writeln!(out, "{line}");
    let _ = out.flush();
}

/// Progress lines until the scan is done, so the caller can show it and tell a stalled scan
/// (a share that stopped answering) from a slow one.
fn progress(sh: &Shared, every: Duration, started: Instant, finished: &AtomicBool) {
    let step = Duration::from_millis(50);
    let mut next = started + every;
    while !finished.load(Ordering::Relaxed) {
        thread::sleep(step);
        if Instant::now() < next {
            continue;
        }
        next += every;
        print_line(&json!({
            "progress": true,
            "files": sh.seen_files.load(Ordering::Relaxed),
            "dirs": sh.seen_dirs.load(Ordering::Relaxed),
            "bytes": sh.seen_bytes.load(Ordering::Relaxed),
            "ms": started.elapsed().as_millis() as u64,
        }));
    }
}

/// Exits when stdin closes: the program that started the scan has quit or cancelled it.
fn watch_stdin() {
    thread::spawn(|| {
        let mut buf = [0u8; 64];
        let mut stdin = io::stdin();
        loop {
            match stdin.read(&mut buf) {
                Ok(0) | Err(_) => std::process::exit(3),
                Ok(_) => {}
            }
        }
    });
}

fn main() {
    let o = parse();
    if o.watch_stdin {
        watch_stdin();
    }
    let started = Instant::now();
    let root_meta = fs::metadata(&o.root);
    let sh = Shared {
        queue: Mutex::new(Queue { tasks: Vec::new(), active: 0, done: false }),
        wake: Condvar::new(),
        stop: AtomicBool::new(false),
        limited: AtomicBool::new(false),
        nodes: Mutex::new(vec![Node::default()]),
        hits: Mutex::new(Vec::new()),
        errors: AtomicU64::new(0),
        unreadable: Mutex::new(Vec::new()),
        other_fs: Mutex::new(Vec::new()),
        links: Mutex::new(HashSet::new()),
        seen_files: AtomicU64::new(0),
        seen_dirs: AtomicU64::new(0),
        seen_bytes: AtomicU64::new(0),
        root_dev: root_meta.as_ref().ok().and_then(device),
        deadline: o.time.map(|t| started + t),
    };
    let chain = if o.mode == Mode::Totals { vec![0] } else { Vec::new() };
    let finished = AtomicBool::new(false);
    match &root_meta {
        Ok(m) if m.is_dir() => {
            sh.queue.lock().unwrap().tasks.push(Task { path: o.root.clone(), depth: 0, chain });
            thread::scope(|s| {
                if let Some(every) = o.progress {
                    let (shared, done) = (&sh, &finished);
                    s.spawn(move || progress(shared, every, started, done));
                }
                let workers: Vec<_> = (0..o.threads).map(|_| s.spawn(|| worker(&sh, &o))).collect();
                for w in workers {
                    let _ = w.join();
                }
                finished.store(true, Ordering::Relaxed);
            });
        }
        Ok(m) if m.is_file() && o.mode == Mode::Totals => {
            let mut nodes = sh.nodes.lock().unwrap();
            nodes[0].size = m.len();
            nodes[0].files = 1;
        }
        Ok(_) => {}
        Err(_) => {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            note(&sh.unreadable, &o.root);
        }
    }
    let mut out = BufWriter::new(io::stdout().lock());
    let nodes = sh.nodes.into_inner().unwrap();
    let complete = !sh.stop.load(Ordering::Relaxed);
    if o.mode == Mode::Totals {
        for n in &nodes {
            let line = json!({"rel": n.rel, "size": n.size, "files": n.files, "dirs": n.dirs, "complete": !n.cut});
            let _ = writeln!(out, "{line}");
        }
    } else {
        for h in sh.hits.into_inner().unwrap() {
            let _ = writeln!(out, "{h}");
        }
    }
    let end = json!({
        "end": true,
        "complete": complete,
        "limited": sh.limited.load(Ordering::Relaxed),
        "errors": sh.errors.load(Ordering::Relaxed),
        "unreadable": sh.unreadable.into_inner().unwrap(),
        "other_fs": sh.other_fs.into_inner().unwrap(),
        "ms": started.elapsed().as_millis() as u64,
    });
    let _ = writeln!(out, "{end}");
    let _ = out.flush();
}

#[cfg(test)]
mod tests {
    use super::glob;

    fn m(p: &str, s: &str) -> bool {
        glob(&p.chars().collect::<Vec<_>>(), &s.chars().collect::<Vec<_>>())
    }

    #[test]
    fn globs_follow_fnmatch() {
        assert!(m("*.iso", "disk.iso"));
        assert!(!m("*.iso", "disk.iso.part"));
        assert!(m("a?c", "abc"));
        assert!(m("[ab]x", "bx"));
        assert!(!m("[!ab]x", "bx"));
        assert!(m("[a-c]1", "b1"));
        assert!(m("[]]", "]"));
        assert!(m("x[", "x["));
        assert!(m("*", ""));
        assert!(m("*a*b", "xxaxxb"));
        assert!(!m("*a*b", "xxaxxc"));
        assert!(m(".kopiaignore", ".kopiaignore"));
    }
}

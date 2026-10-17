//! kopialens-scan: answers KopiaLens's disk questions quickly on every platform.
//!
//!   kopialens-scan totals <root> [--depth N]
//!       size, file and folder counts for <root> and for every folder up to N levels below it
//!   kopialens-scan find <root> <pattern> [--dirs] [--fold] [--limit N]
//!       entries whose name matches a glob (`*`, `?`, `[...]`, `[!...]`, like Python's fnmatch)
//!
//! Shared options: `--time-ms N` (stop after N ms; unfinished folders report `complete: false`),
//! `--threads N`, `--skip <path>` (repeatable, a folder never entered).
//! Output: one JSON object per line, then `{"end": true, ...}`. Symlinks and junctions are never
//! followed or counted; only regular files count.

use std::fs;
use std::io::{self, BufWriter, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

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
    skip: Vec<PathBuf>,
    pattern: Vec<char>,
    dirs_only: bool,
    fold: bool,
    limit: usize,
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
}

fn usage() -> ! {
    eprintln!("usage: kopialens-scan totals <root> [--depth N] | find <root> <pattern> [--dirs] [--fold] [--limit N]");
    eprintln!("       shared: [--time-ms N] [--threads N] [--skip <path>]...");
    std::process::exit(2);
}

fn number(v: Option<String>) -> u64 {
    v.and_then(|s| s.parse().ok()).unwrap_or_else(|| usage())
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
    let pattern = if mode == Mode::Find { args.next().unwrap_or_else(|| usage()) } else { String::new() };
    let auto = thread::available_parallelism().map(|n| n.get()).unwrap_or(4).min(8);
    let mut o = Opts {
        mode,
        root,
        depth: 1,
        time: None,
        threads: auto,
        skip: Vec::new(),
        pattern: Vec::new(),
        dirs_only: false,
        fold: false,
        limit: usize::MAX,
    };
    while let Some(a) = args.next() {
        match a.as_str() {
            "--depth" => o.depth = number(args.next()) as usize,
            "--time-ms" => o.time = Some(Duration::from_millis(number(args.next()))),
            "--threads" => o.threads = (number(args.next()) as usize).clamp(1, 64),
            "--skip" => o.skip.push(PathBuf::from(args.next().unwrap_or_else(|| usage()))),
            "--dirs" => o.dirs_only = true,
            "--fold" => o.fold = true,
            "--limit" => o.limit = (number(args.next()) as usize).max(1),
            _ => usage(),
        }
    }
    o.pattern = if o.fold { pattern.to_lowercase() } else { pattern }.chars().collect();
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
    glob(&o.pattern, &chars)
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

/// Reads one folder: adds its files to every folder in its chain and returns its subfolders.
fn visit(sh: &Shared, o: &Opts, t: &Task) -> Vec<Task> {
    let rd = match fs::read_dir(&t.path) {
        Ok(rd) => rd,
        Err(_) => {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            return Vec::new();
        }
    };
    let (mut size, mut files) = (0u64, 0u64);
    let mut subdirs: Vec<(PathBuf, String)> = Vec::new();
    for ent in rd {
        let Ok(ent) = ent else {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            continue;
        };
        let Ok(ft) = ent.file_type() else {
            sh.errors.fetch_add(1, Ordering::Relaxed);
            continue;
        };
        if ft.is_symlink() {
            continue;
        }
        let name = ent.file_name().to_string_lossy().into_owned();
        if ft.is_dir() {
            let path = ent.path();
            if o.skip.iter().any(|s| *s == path) {
                continue;
            }
            if o.mode == Mode::Find && matches(o, &name) {
                add_hit(sh, o, json!({"path": path.to_string_lossy(), "dir": true}));
            }
            subdirs.push((path, name));
        } else if ft.is_file() {
            if o.mode == Mode::Totals {
                // On Windows the listing already carries the size; elsewhere this is one lstat.
                match ent.metadata() {
                    Ok(m) => size += m.len(),
                    Err(_) => {
                        sh.errors.fetch_add(1, Ordering::Relaxed);
                    }
                }
                files += 1;
            } else if !o.dirs_only && matches(o, &name) {
                let len = ent.metadata().map(|m| m.len()).ok();
                add_hit(sh, o, json!({"path": ent.path().to_string_lossy(), "dir": false, "size": len}));
            }
        }
    }
    if o.mode == Mode::Find {
        return subdirs.into_iter().map(|(path, _)| Task { path, depth: t.depth + 1, chain: Vec::new() }).collect();
    }
    let mut nodes = sh.nodes.lock().unwrap();
    for &id in &t.chain {
        let n = &mut nodes[id];
        n.size += size;
        n.files += files;
        n.dirs += subdirs.len() as u64;
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

fn worker(sh: &Shared, o: &Opts, deadline: Option<Instant>) {
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
        if deadline.is_some_and(|d| Instant::now() >= d) {
            sh.stop.store(true, Ordering::Relaxed);
        }
        let next = if sh.stop.load(Ordering::Relaxed) {
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

fn main() {
    let o = parse();
    let started = Instant::now();
    let deadline = o.time.map(|t| started + t);
    let sh = Shared {
        queue: Mutex::new(Queue { tasks: Vec::new(), active: 0, done: false }),
        wake: Condvar::new(),
        stop: AtomicBool::new(false),
        limited: AtomicBool::new(false),
        nodes: Mutex::new(vec![Node::default()]),
        hits: Mutex::new(Vec::new()),
        errors: AtomicU64::new(0),
    };
    let chain = if o.mode == Mode::Totals { vec![0] } else { Vec::new() };
    match fs::metadata(&o.root) {
        Ok(m) if m.is_dir() => {
            sh.queue.lock().unwrap().tasks.push(Task { path: o.root.clone(), depth: 0, chain });
            thread::scope(|s| {
                for _ in 0..o.threads {
                    s.spawn(|| worker(&sh, &o, deadline));
                }
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

//! `klens --installer-path add|remove user|machine <folder>`: the installer's PATH edit.
//! The registry value is read and written as stored: no length limit, `%VAR%` entries and the value
//! type kept, entries matched whole. Nothing is written after a failed read.
//! Exit codes: 0 done (or nothing to do), 1 couldn't read PATH, 2 couldn't write it, 64 bad arguments.

use std::ffi::{c_void, OsStr};
use std::os::windows::ffi::OsStrExt;
use std::ptr::{null, null_mut};

type Hkey = isize;
const HKEY_CURRENT_USER: Hkey = 0x8000_0001u32 as i32 as isize;
const HKEY_LOCAL_MACHINE: Hkey = 0x8000_0002u32 as i32 as isize;
const KEY_QUERY_VALUE: u32 = 0x0001;
const KEY_SET_VALUE: u32 = 0x0002;
const REG_SZ: u32 = 1;
const REG_EXPAND_SZ: u32 = 2;
const ERROR_SUCCESS: i32 = 0;
const ERROR_FILE_NOT_FOUND: i32 = 2;
const ERROR_MORE_DATA: i32 = 234;
const HWND_BROADCAST: isize = 0xffff;
const WM_SETTINGCHANGE: u32 = 0x001A;
const SMTO_ABORTIFHUNG: u32 = 0x0002;
const SEMI: u16 = b';' as u16;

#[link(name = "advapi32")]
extern "system" {
    fn RegCreateKeyExW(key: Hkey, sub: *const u16, reserved: u32, class: *const u16, options: u32,
                       sam: u32, security: *const c_void, result: *mut Hkey, disposition: *mut u32) -> i32;
    fn RegQueryValueExW(key: Hkey, name: *const u16, reserved: *mut u32, kind: *mut u32, data: *mut u8,
                        len: *mut u32) -> i32;
    fn RegSetValueExW(key: Hkey, name: *const u16, reserved: u32, kind: u32, data: *const u8, len: u32) -> i32;
    fn RegCloseKey(key: Hkey) -> i32;
}

#[link(name = "user32")]
extern "system" {
    fn SendMessageTimeoutW(hwnd: isize, msg: u32, wparam: usize, lparam: isize, flags: u32, timeout: u32,
                           result: *mut usize) -> isize;
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}

/// Compare form of one entry: trimmed, no trailing slash, lower case.
fn norm(entry: &[u16]) -> String {
    String::from_utf16_lossy(entry).trim().trim_end_matches(['\\', '/']).to_lowercase()
}

/// PATH with `dir` added at the end, or None when it is already there. A trailing ";" is kept.
pub fn added(path: &[u16], dir: &[u16]) -> Option<Vec<u16>> {
    let want = norm(dir);
    if path.split(|c| *c == SEMI).any(|e| norm(e) == want) {
        return None;
    }
    let mut out = path.to_vec();
    if out.is_empty() {
        out.extend_from_slice(dir);
    } else if out.last() == Some(&SEMI) {
        out.extend_from_slice(dir);
        out.push(SEMI);
    } else {
        out.push(SEMI);
        out.extend_from_slice(dir);
    }
    Some(out)
}

/// PATH without any entry equal to `dir`, or None when it has none.
pub fn removed(path: &[u16], dir: &[u16]) -> Option<Vec<u16>> {
    let want = norm(dir);
    let kept: Vec<&[u16]> = path.split(|c| *c == SEMI).filter(|e| norm(e) != want).collect();
    if kept.len() == path.split(|c| *c == SEMI).count() {
        return None;
    }
    Some(kept.join(&SEMI))
}

struct Key(Hkey);

impl Drop for Key {
    fn drop(&mut self) {
        unsafe { RegCloseKey(self.0) };
    }
}

fn open(machine: bool) -> Result<Key, String> {
    let (root, sub) = if machine {
        (HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    } else {
        (HKEY_CURRENT_USER, "Environment")
    };
    let mut key: Hkey = 0;
    let rc = unsafe {
        RegCreateKeyExW(root, wide(sub).as_ptr(), 0, null(), 0, KEY_QUERY_VALUE | KEY_SET_VALUE, null(),
                        &mut key, null_mut())
    };
    if rc == ERROR_SUCCESS { Ok(Key(key)) } else { Err(format!("couldn't open the environment key (error {rc})")) }
}

/// PATH exactly as stored with its type; None when the value doesn't exist.
fn read(key: &Key) -> Result<Option<(u32, Vec<u16>)>, String> {
    let name = wide("Path");
    let mut need: u32 = 0;
    for _ in 0..10 {
        let mut buf = vec![0u16; need as usize / 2 + 2];
        let mut len = (buf.len() * 2) as u32;
        let mut kind = 0u32;
        let rc = unsafe {
            RegQueryValueExW(key.0, name.as_ptr(), null_mut(), &mut kind, buf.as_mut_ptr().cast(), &mut len)
        };
        match rc {
            ERROR_SUCCESS => {
                if kind != REG_SZ && kind != REG_EXPAND_SZ {
                    return Err(format!("PATH isn't text (registry type {kind})"));
                }
                buf.truncate(len as usize / 2);
                while buf.last() == Some(&0) {
                    buf.pop();
                }
                return Ok(Some((kind, buf)));
            }
            ERROR_FILE_NOT_FOUND => return Ok(None),
            ERROR_MORE_DATA => need = len,
            rc => return Err(format!("couldn't read PATH (error {rc})")),
        }
    }
    Err("couldn't read PATH (it kept changing)".into())
}

fn write(key: &Key, kind: u32, value: &[u16]) -> Result<(), String> {
    let mut data = value.to_vec();
    data.push(0);
    let rc = unsafe {
        RegSetValueExW(key.0, wide("Path").as_ptr(), 0, kind, data.as_ptr().cast(), (data.len() * 2) as u32)
    };
    if rc == ERROR_SUCCESS { Ok(()) } else { Err(format!("couldn't write PATH (error {rc})")) }
}

pub fn run(args: &[std::ffi::OsString]) -> i32 {
    let usage = || {
        eprintln!("usage: klens --installer-path add|remove user|machine <folder>");
        64
    };
    let [action, scope, dir] = args else { return usage() };
    let add = match action.to_str() {
        Some("add") => true,
        Some("remove") => false,
        _ => return usage(),
    };
    let machine = match scope.to_str() {
        Some("machine") => true,
        Some("user") => false,
        _ => return usage(),
    };
    let dir: Vec<u16> = OsStr::new(dir).encode_wide().collect();
    if norm(&dir).is_empty() {
        return usage();
    }

    let key = match open(machine) {
        Ok(key) => key,
        Err(e) => {
            eprintln!("klens: {e}");
            return 1;
        }
    };
    let (kind, path) = match read(&key) {
        Ok(Some(found)) => found,
        Ok(None) => (REG_EXPAND_SZ, Vec::new()),
        Err(e) => {
            eprintln!("klens: {e}; PATH left unchanged");
            return 1;
        }
    };
    let new = if add { added(&path, &dir) } else { removed(&path, &dir) };
    let Some(new) = new else { return 0 };
    if let Err(e) = write(&key, kind, &new) {
        eprintln!("klens: {e}");
        return 2;
    }
    let env = wide("Environment");
    unsafe {
        SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, env.as_ptr() as isize, SMTO_ABORTIFHUNG, 5000,
                            null_mut());
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(s: &str) -> Vec<u16> {
        s.encode_utf16().collect()
    }

    fn s(v: Option<Vec<u16>>) -> Option<String> {
        v.map(|v| String::from_utf16(&v).unwrap())
    }

    #[test]
    fn add_then_remove_restores_the_exact_value() {
        for before in ["%SystemRoot%\\system32;C:\\Tools", "%SystemRoot%\\system32;C:\\Tools;", ""] {
            let after = added(&w(before), &w(r"C:\KL\bin")).unwrap();
            assert_eq!(s(removed(&after, &w(r"C:\KL\bin"))).unwrap(), before);
        }
    }

    #[test]
    fn entries_match_whole_and_ignore_case_and_trailing_slash() {
        assert_eq!(added(&w(r"C:\a;c:\kl\BIN\;D:\x"), &w(r"C:\KL\bin")), None);
        assert_eq!(removed(&w(r"C:\KL\bin2;C:\KL\bin-old"), &w(r"C:\KL\bin")), None);
        assert_eq!(s(removed(&w(r"C:\a;C:\KL\bin;C:\KL\bin2"), &w(r"C:\KL\bin"))).unwrap(), r"C:\a;C:\KL\bin2");
    }
}

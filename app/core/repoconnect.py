"""Connect KopiaLens to a kopia repository - every storage type kopia supports.

One flow for all of them: KopiaLens writes a config file (kopia's own folder or ``<data>/configs/``,
see ``config_location``) and runs ``kopia repository connect <provider> <flags>``. The repository
password travels in ``KOPIA_PASSWORD`` and storage secrets in kopia's own environment variables or
stdin (never argv, which is visible in a process list); kopia then persists the password
itself (Credential Manager on Windows, ``<config>.kopia-password`` elsewhere) - KopiaLens never
stores a repo password.

An EXISTING connection (KopiaUI's ``repository-*.config``, the CLI default, a config somebody
copied here) is ATTACHED, not copied: kopia's OS-keyring entry is keyed by the config's full path
(``internal/passwordpersist``), so a copy would silently lose the remembered password.

The field specs mirror kopia's own CLI flags (``cli/storage_*.go``) because the CLI is what we
drive. The screens render their forms from the same specs, so a new field is added in one place.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import config

from . import audit, kopiabin, repos
from .applog import get_logger
from .kopia import MISSING_KOPIA

log = get_logger("repoconnect")

_CONNECT_LOCK = threading.Lock()   # id allocation + record insert must not interleave


def kopia_config_dir() -> str:
    """Where kopia itself keeps config files (``kopia --config-file`` with a bare name and
    KopiaUI's per-connection files both live here)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "kopia")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "kopia")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "kopia")


# Where a KopiaLens-CREATED connection keeps its kopia config file.
CONFIG_LOCATIONS = ("kopia", "data")


def _location_setting() -> str:
    try:
        from ..settings import load_settings
        return str(load_settings().get("kopialens", {}).get("kopia_config_location") or "").strip()
    except Exception:  # noqa: BLE001 - a settings read must not break a connect
        return ""


def config_location() -> str:
    """`kopia` = kopia's own config folder, so KopiaUI and the kopia command line see the
    connection too; `data` = KopiaLens's data folder, which is what a container wants (the data
    folder is a mounted volume, kopia's folder inside a container is not).

    Precedence mirrors the bind default: env beats an explicit setting, which beats the profile
    default (tray = desktop = kopia's folder; bare python/systemd/Docker = server = data folder)."""
    env = (os.environ.get("KOPIALENS_KOPIA_CONFIG_LOCATION") or "").strip().lower()
    if env in CONFIG_LOCATIONS:
        return env
    chosen = _location_setting().lower()
    if chosen in CONFIG_LOCATIONS:
        return chosen
    # Lazy: core.auth owns the launch profile, and importing it at module level drags in the whole
    # auth/settings chain for anyone who only wants a provider list.
    try:
        from .auth import profile
        return "kopia" if profile() == "desktop" else "data"
    except Exception:  # noqa: BLE001
        return "kopia"


def managed_config_dir() -> str:
    """The folder a created connection's config file lives in, per `config_location()`."""
    if config_location() == "data":
        return os.path.join(config.DATA_DIR, "configs")
    return kopia_config_dir()


def managed_config_path(rid: str) -> str:
    """The config file for a connection KopiaLens creates. In kopia's folder it uses KopiaUI's own
    `repository-*.config` convention (so KopiaUI lists it as a connection), in our data folder the
    shorter `repo<id>.config`."""
    if config_location() == "data":
        return os.path.join(managed_config_dir(), f"repo{rid}.config")
    return os.path.join(kopia_config_dir(), f"repository-kopialens-{rid}.config")


def is_managed_config(config_file: str | None) -> bool:
    """True when this path is a config KopiaLens created (either location). Used to decide whether
    removing a repo may delete the file - a user's own config is never deleted."""
    if not config_file:
        return False
    try:
        path = os.path.abspath(config_file)
        name = os.path.basename(path)
        in_kopia = os.path.normcase(os.path.dirname(path)) == os.path.normcase(
            os.path.abspath(kopia_config_dir()))
        in_data = os.path.normcase(os.path.dirname(path)) == os.path.normcase(
            os.path.abspath(os.path.join(config.DATA_DIR, "configs")))
        if not (in_kopia or in_data):
            return False
        return bool(re.match(r"^(repo\d{1,12}\.config|repository-kopialens-\d{1,12}\.config)$", name))
    except Exception:  # noqa: BLE001
        return False


def _field(key: str, flag: str, label: str, kind: str = "text", *, required: bool = False,
           secret: bool = False, default=None, help: str = "", advanced: bool = False,
           env: str = "") -> dict:
    # `env`: kopia reads this secret from that environment variable (cli/storage_*.go `.Envar`),
    # so it never has to appear in argv.
    return {"key": key, "flag": flag, "label": label, "kind": kind, "required": required,
            "secret": secret, "default": default, "help": help, "advanced": advanced, "env": env}


def _bool(key: str, flag: str, label: str, *, default: bool = False, help: str = "",
          advanced: bool = False) -> dict:
    f = _field(key, flag, label, "bool", default=default, help=help, advanced=advanced)
    # kingpin answers --no-<flag> for a bool whose default is true; a False value must send that
    # form or the flag keeps its default instead of the user's choice.
    f["negate"] = f"--no-{flag.lstrip('-')}"
    return f


PROVIDERS: dict[str, dict] = {
    "filesystem": {
        "label": "Local folder or NAS",
        "provider": "filesystem",
        "summary": "A folder on this machine, a mounted drive, or a mapped network share.",
        "cloud": False,
        "fields": [
            _field("path", "--path", "Folder", "path", required=True,
                   help="The folder holding the repository, or where it will be created."),
            _bool("flat", "--flat", "Flat layout", advanced=True,
                  help="Store blobs directly in the folder instead of the sharded default."),
            _field("owner_uid", "--owner-uid", "Owner UID", advanced=True),
            _field("owner_gid", "--owner-gid", "Owner GID", advanced=True),
            _field("file_mode", "--file-mode", "File mode", advanced=True),
            _field("dir_mode", "--dir-mode", "Folder mode", advanced=True),
        ],
    },
    "sftp": {
        "label": "SFTP server",
        "provider": "sftp",
        "summary": "A repository on any SSH/SFTP server.",
        "cloud": False,
        "requires_any": [("[sftp_password]", "[keyfile]", "[key_data]"),
                         ("[known_hosts]", "[known_hosts_data]")],
        # kopia skips both groups when the system ssh command is used (cli/storage_sftp.go)
        "requires_any_unless": "external",
        "fields": [
            _field("host", "--host", "Server", required=True),
            _field("port", "--port", "Port", "int", default=22),
            _field("username", "--username", "User name", required=True),
            _field("path", "--path", "Folder on the server", required=True),
            _field("sftp_password", "--sftp-password", "Password", "password", secret=True),
            _field("keyfile", "--keyfile", "Private key file", "file", advanced=True),
            _field("key_data", "--key-data", "Private key data", "text", secret=True, advanced=True),
            _field("known_hosts", "--known-hosts", "Known hosts file", "file", advanced=True),
            _field("known_hosts_data", "--known-hosts-data", "Known hosts entries", "text", advanced=True),
            _bool("embed_credentials", "--embed-credentials", "Embed key and known hosts in the config",
                  advanced=True),
            _bool("external", "--external", "Use the system SSH command", advanced=True,
                  help="Reuse your existing SSH setup (ssh-agent, config) instead of a key or password."),
            _field("ssh_command", "--ssh-command", "SSH command", default="ssh", advanced=True),
            _field("ssh_args", "--ssh-args", "SSH arguments", advanced=True),
            _bool("flat", "--flat", "Flat layout", advanced=True),
        ],
    },
    "s3": {
        "label": "Amazon S3 or compatible",
        "provider": "s3",
        "summary": "Amazon S3, MinIO, Cloudflare R2, Wasabi, Backblaze B2's S3 API and others.",
        "cloud": True,
        "requires_any": [("[access_key]", "[secret_access_key]")],
        "fields": [
            _field("bucket", "--bucket", "Bucket", required=True),
            _field("endpoint", "--endpoint", "Endpoint", default="s3.amazonaws.com",
                   help="Change this for anything that is not Amazon S3."),
            _field("region", "--region", "Region"),
            _field("access_key", "--access-key", "Access key ID", required=True),
            _field("secret_access_key", "--secret-access-key", "Secret access key", "password",
                   required=True, secret=True, env="AWS_SECRET_ACCESS_KEY"),
            _field("session_token", "--session-token", "Session token", "password", secret=True,
                   advanced=True, env="AWS_SESSION_TOKEN"),
            _field("prefix", "--prefix", "Prefix",
                   help="Folder inside the bucket. End it with a slash to use it as a directory."),
            _bool("disable_tls", "--disable-tls", "Use plain HTTP", advanced=True),
            _bool("disable_tls_verification", "--disable-tls-verification",
                  "Skip the TLS certificate check", advanced=True),
            _field("root_ca_pem_path", "--root-ca-pem-path", "Certificate authority file", "file",
                   advanced=True),
        ],
    },
    "b2": {
        "label": "Backblaze B2",
        "provider": "b2",
        "summary": "A Backblaze B2 bucket (application key ID and key).",
        "cloud": True,
        "fields": [
            _field("bucket", "--bucket", "Bucket", required=True),
            _field("key_id", "--key-id", "Key ID", required=True),
            _field("key", "--key", "Application key", "password", required=True, secret=True,
                   env="B2_KEY"),
            _field("prefix", "--prefix", "Prefix"),
        ],
    },
    "gcs": {
        "label": "Google Cloud Storage",
        "provider": "gcs",
        "summary": "A Google Cloud Storage bucket.",
        "cloud": True,
        "fields": [
            _field("bucket", "--bucket", "Bucket", required=True),
            _field("prefix", "--prefix", "Prefix"),
            _field("credentials_file", "--credentials-file", "Service account JSON", "file",
                   advanced=True,
                   help="Leave empty to use the machine's application default credentials."),
            _bool("embed_credentials", "--embed-credentials", "Embed the credentials in the config",
                  advanced=True),
            _bool("read_only", "--read-only", "Connect read-only", advanced=True),
        ],
    },
    "azure": {
        "label": "Azure Blob Storage",
        "provider": "azure",
        "storage_type": "azureBlob",
        "summary": "An Azure storage account container, with a key, a SAS token, or a service principal.",
        "cloud": True,
        "requires_any": [("[storage_key]", "[sas_token]", "[tenant_id]")],
        "fields": [
            _field("container", "--container", "Container", required=True),
            _field("storage_account", "--storage-account", "Storage account", required=True),
            _field("storage_key", "--storage-key", "Account key", "password", secret=True,
                   env="AZURE_STORAGE_KEY"),
            _field("sas_token", "--sas-token", "SAS token", "password", secret=True,
                   env="AZURE_STORAGE_SAS_TOKEN"),
            _field("storage_domain", "--storage-domain", "Storage domain", advanced=True),
            _field("prefix", "--prefix", "Prefix"),
            _field("tenant_id", "--tenant-id", "Service principal tenant ID", advanced=True),
            _field("client_id", "--client-id", "Service principal client ID", advanced=True),
            _field("client_secret", "--client-secret", "Service principal secret", "password",
                   secret=True, advanced=True, env="AZURE_CLIENT_SECRET"),
            _field("client_cert", "--client-cert", "Service principal certificate", "file",
                   advanced=True),
            _field("azure_federated_token_file", "--azure-federated-token-file",
                   "Federated token file", "file", advanced=True),
        ],
    },
    "webdav": {
        "label": "WebDAV server",
        "provider": "webdav",
        "summary": "Any WebDAV server (Nextcloud, ownCloud, a NAS, ...).",
        "cloud": False,
        "fields": [
            _field("url", "--url", "URL", required=True),
            _field("webdav_username", "--webdav-username", "User name"),
            _field("webdav_password", "--webdav-password", "Password", "password", secret=True,
                   env="KOPIA_WEBDAV_PASSWORD"),
            _bool("atomic_writes", "--atomic-writes", "Provider supports atomic writes", advanced=True),
            _bool("flat", "--flat", "Flat layout", advanced=True),
        ],
    },
    "rclone": {
        "label": "Rclone remote",
        "provider": "rclone",
        "summary": "Anything rclone can reach (Google Drive, OneDrive, Dropbox, ...).",
        "cloud": True,
        "fields": [
            _field("remote_path", "--remote-path", "Remote path", required=True,
                   help="An rclone remote and path, for example 'mydrive:kopia'."),
            _field("rclone_exe", "--rclone-exe", "rclone program", "file", advanced=True),
            _field("rclone_args", "--rclone-args", "Extra rclone arguments", advanced=True),
            _field("embed_rclone_config", "--embed-rclone-config", "Embed the rclone config", "file",
                   advanced=True),
            _bool("atomic_writes", "--atomic-writes", "Provider supports atomic writes",
                  default=True, advanced=True),
        ],
    },
    "gdrive": {
        "label": "Google Drive",
        "provider": "gdrive",
        "summary": "A Google Drive folder, with a service account or application default credentials.",
        "cloud": True,
        "fields": [
            _field("folder_id", "--folder-id", "Folder ID", required=True,
                   help="The Drive folder's ID, from its share link."),
            _field("credentials_file", "--credentials-file", "Service account JSON", "file",
                   advanced=True),
            _bool("embed_credentials", "--embed-credentials", "Embed the credentials in the config",
                  advanced=True),
            _bool("read_only", "--read-only", "Connect read-only", advanced=True),
        ],
    },
    "server": {
        "label": "Kopia repository server",
        "provider": "server",
        "summary": "A repository another kopia server already has open (KopiaUI or a headless server).",
        "cloud": False,
        "can_create": False,
        "fields": [
            _field("url", "--url", "Server URL", required=True,
                   help="For example https://127.0.0.1:51515."),
            _field("server_cert_fingerprint", "--server-cert-fingerprint", "Certificate fingerprint",
                   advanced=True),
            _field("username", "--override-username", "Username", advanced=True,
                   help="The account's user name on the kopia server, when it isn't this computer's."),
            _field("hostname", "--override-hostname", "Hostname", advanced=True,
                   help="The host part of the account (user@host), when it isn't this computer's name."),
        ],
    },
    "token": {
        "label": "Repository token",
        "provider": "from-config",
        "summary": "A connection token made by `kopia repository status --json` "
                   "or `kopia repository connect --token`.",
        "cloud": False,
        "can_create": False,
        "password": False,   # the token carries the password itself
        "requires_any": [("[token]", "[token_file]")],
        "fields": [
            _field("token", "--token", "Token", "text", secret=True),
            _field("token_file", "--token-file", "Token file", "file"),
        ],
    },
}


def provider_list() -> list[dict]:
    """The provider specs for the screens (/ UI) - one source with both flows."""
    out = []
    for key, spec in PROVIDERS.items():
        out.append({
            "key": key,
            "label": spec["label"],
            "summary": spec.get("summary", ""),
            "cloud": bool(spec.get("cloud")),
            "password": spec.get("password", True),
            # A repository server and a token are CONNECTIONS to an existing repository - neither
            # can create one, so the create screen must not offer them.
            "can_create": bool(spec.get("can_create", True)),
            # "fill at least one of these" groups, by field key, for the form
            "requires_any": [[g.strip("[]") for g in group] for group in spec.get("requires_any", [])],
            "requires_any_unless": spec.get("requires_any_unless", ""),
            "fields": [dict(f) for f in spec["fields"]],
        })
    return out


def _spec(provider: str) -> dict:
    spec = PROVIDERS.get(provider)
    if not spec:
        raise ValueError(f"unknown storage type: {provider!r}")
    return spec


def validate_fields(provider: str, fields: dict) -> list[str]:
    """Missing-required messages for a provider form. Returns [] when the form is complete."""
    spec = _spec(provider)
    missing = []
    for f in spec["fields"]:
        if not f["required"]:
            continue
        value = fields.get(f["key"])
        if f["kind"] == "bool":
            continue
        if value is None or str(value).strip() == "":
            missing.append(f"{f['label']} is required.")
    unless = spec.get("requires_any_unless")
    groups = [] if (unless and fields.get(unless)) else spec.get("requires_any", [])
    for group in groups:
        keys = [g.strip("[]") for g in group]
        if not any(str(fields.get(k) or "").strip() for k in keys):
            labels = " or ".join(next(f["label"] for f in spec["fields"] if f["key"] == k)
                                 for k in keys)
            missing.append(f"{labels} is required.")
    return missing


# `kopia repository create` format options (cli/command_repository_create.go). They fix the
# repository's on-disk format and CANNOT be changed later, so they pass through only when the user
# asks; when absent kopia's own defaults apply (--block-hash/--encryption/--object-splitter etc.).
CREATE_OPTIONS = {
    "block_hash": "--block-hash",
    "encryption": "--encryption",
    "ecc": "--ecc",
    "ecc_overhead_percent": "--ecc-overhead-percent",
    "object_splitter": "--object-splitter",
    "format_version": "--format-version",
}


def build_connect_args(provider: str, fields: dict, config_file: str, password: str | None) -> list[str]:
    """The full argv for `kopia repository connect`. The repository password is NOT part of it
    (KOPIA_PASSWORD is set on the child instead - argv is visible in a process list)."""
    return _build_args("connect", provider, fields, config_file, None)


def build_create_args(provider: str, fields: dict, config_file: str, options: dict | None = None) -> list[str]:
    """The full argv for `kopia repository create` - the same storage flags plus the format
    options. kopia refuses a non-empty storage location itself."""
    return _build_args("create", provider, fields, config_file, options)


def _build_args(verb: str, provider: str, fields: dict, config_file: str,
                options: dict | None) -> list[str]:
    spec = _spec(provider)
    if verb == "create" and not spec.get("can_create", True):
        raise ValueError(f"{spec['label']} cannot host a new repository - connect to one instead.")
    args = [kopiabin.exe(), "--config-file", config_file, "repository", verb, spec["provider"]]
    for f in spec["fields"]:
        value = fields.get(f["key"])
        if f["kind"] == "bool":
            if value is None:
                continue
            want = bool(value)
            if want != bool(f.get("default", False)):
                args.append(f["flag"] if want else f.get("negate") or f["flag"])
            continue
        if value is None or str(value).strip() == "":
            continue
        if f.get("env") or (provider == "token" and f["key"] == "token"):
            continue                 # carried by `secret_inputs`, never argv
        args += [f["flag"], str(value)]
    if provider == "token" and str(fields.get("token") or "").strip():
        args.append("--token-stdin")
    for key, flag in CREATE_OPTIONS.items():
        value = (options or {}).get(key)
        if value is None or str(value).strip() == "" or str(value).strip() == "0":
            continue
        args += [flag, str(value)]
    # kopia's own update check is not ours to run (it costs a network round trip and prints a
    # notice nobody sees here); KopiaLens checks for ITS updates separately.
    args.append("--no-check-for-updates")
    return args


def secret_inputs(provider: str, fields: dict) -> tuple[dict, str | None]:
    """``(env, stdin)`` for the secrets `_build_args` leaves out of argv: kopia's own environment
    variables, and the repository token over ``--token-stdin``."""
    spec = _spec(provider)
    env = {f["env"]: str(fields[f["key"]]) for f in spec["fields"]
           if f.get("env") and str(fields.get(f["key"]) or "").strip()}
    token = str(fields.get("token") or "").strip() if provider == "token" else ""
    return env, (token or None)


def _run(args: list[str], password: str | None, timeout: float = 600.0,
         extra_env: dict | None = None, stdin_text: str | None = None) -> dict:
    """Run one connect/attach command. Secrets go in the environment or stdin only; with no stdin
    text, stdin is DEVNULL so a password prompt fails instantly instead of hanging."""
    env = dict(os.environ)
    env["KOPIA_CHECK_FOR_UPDATES"] = "false"
    env.update(extra_env or {})
    if password:
        env["KOPIA_PASSWORD"] = password
    if not kopiabin.resolve():
        return {"returncode": 1, "stdout": "", "stderr": MISSING_KOPIA, "command": args}
    t0 = time.time()
    try:
        stdin_kw = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
        res = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            **stdin_kw,
        )
    except subprocess.TimeoutExpired:
        return {"returncode": -9, "stdout": "", "stderr": f"kopia timed out after {timeout:.0f}s",
                "command": args, "elapsed": time.time() - t0}
    except OSError as exc:
        return {"returncode": 1, "stdout": "", "stderr": f"{MISSING_KOPIA} ({exc})",
                "command": args, "elapsed": time.time() - t0}
    return {"returncode": res.returncode, "stdout": res.stdout or "", "stderr": res.stderr or "",
            "command": args, "elapsed": time.time() - t0}


_CONFIG_SUFFIXES = ("", ".kopia-password", ".update-info.json", ".mlock")


def _config_siblings(config_file: str) -> list[str]:
    """A config file and the siblings kopia keeps beside it that exist on disk."""
    return [config_file + s for s in _CONFIG_SUFFIXES if os.path.isfile(config_file + s)]


def _remove_config_files(config_file: str) -> None:
    """Remove a config file and the siblings kopia keeps beside it (persisted password, update
    info, lock). Used for what a FAILED connect left behind, and for a managed config the user
    asked to delete with the repo. Never called on a config KopiaLens did not create."""
    for suffix in _CONFIG_SUFFIXES:
        path = config_file + suffix
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def connect(provider: str, fields: dict, password: str, name: str = "", short_name: str = "",
            counts_as_backup: bool = True) -> dict:
    """Connect a repository KopiaLens manages the config for, and record it."""
    return _establish("connect", provider, fields, password, name, short_name, counts_as_backup)


def create(provider: str, fields: dict, password: str, name: str = "", short_name: str = "",
           counts_as_backup: bool = True, options: dict | None = None) -> dict:
    """Create a NEW repository in the given storage and record it (kopia's `repository create`
    initializes the storage and connects this config to it in one step).

    The storage location must be EMPTY - kopia refuses otherwise, and that refusal is surfaced
    as-is. This is the one flow that writes to a user's storage rather than only reading it."""
    return _establish("create", provider, fields, password, name, short_name, counts_as_backup,
                      options)


def _establish(verb: str, provider: str, fields: dict, password: str, name: str, short_name: str,
               counts_as_backup: bool, options: dict | None = None) -> dict:
    """The shared connect/create path: validate, allocate an id + managed config, run kopia, record.

    Raises ValueError with kopia's own message when it fails - nothing is recorded and the
    half-written config file is removed."""
    fields = dict(fields or {})
    missing = validate_fields(provider, fields)
    if missing:
        raise ValueError(" ".join(missing))
    spec = _spec(provider)
    if spec.get("password", True) and not (password or "").strip():
        raise ValueError("The repository password is required.")
    # The id + the managed config path are RESERVED before the kopia call: two overlapping
    # connects must never share an identity or a file (a failed connect just burns a number).
    with _CONNECT_LOCK:
        rid = repos.reserve_id()
        config_file = managed_config_path(rid)
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
    log.info("repo id %s reserved for a new connection", rid)
    args = _build_args(verb, provider, fields, config_file, options)
    extra_env, stdin_text = secret_inputs(provider, fields)
    log.info("%s repo=%s provider=%s config=%s", verb, rid, provider, config_file)
    res = _run(args, password, extra_env=extra_env, stdin_text=stdin_text)
    if res["returncode"] != 0:
        _remove_config_files(config_file)
        detail = _last_meaningful_line((res.get("stderr") or "") + "\n" + (res.get("stdout") or ""))
        audit.audit("generated", f"repo-{verb}", repo=rid,
                    summary={"provider": provider, "ok": False, "config": config_file},
                    rc=res["returncode"])
        raise ValueError(detail or f"kopia could not {verb} to the repository (exit {res['returncode']}).")
    record = repos.add_record({
        "id": rid, "name": name or f"{spec['label']}", "short_name": short_name,
        "kind": "kopia", "config_file": config_file,
        "source": "created" if verb == "create" else "connected",
        "counts_as_backup": bool(counts_as_backup),
    })
    repos.capture_status(rid)
    status = repos.storage_info(rid)
    log.warning("repo=%s %s (%s, %s)", rid, "created" if verb == "create" else "connected",
                provider, status.get("type") or "?")
    audit.audit("executed", f"repo-{verb}", repo=rid,
                summary={"provider": provider, "ok": True, "config": config_file,
                         "storage": status.get("type")})
    return record


def _last_meaningful_line(text: str) -> str:
    """kopia reports the real error last; the earlier lines are progress noise. Kept to one line so
    it can go straight into a UI message."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.lower().startswith(("to disable", "alternatively")):
            continue   # the update-check note that follows a success/failure
        return ln[:300]
    return ""


def read_config_file(config_file: str) -> dict:
    """kopia's own config JSON, or {} when it cannot be read. A connection config carries either
    ``storage`` (a storage connection) or ``apiServer`` (a repository server connection)."""
    try:
        with open(config_file, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - unreadable config is a normal answer here
        return {}


def describe_config(config_file: str) -> dict:
    """Type facts for a config file, read directly (no spawn).

    The file DOES hold credentials (kopia marshals the storage options as-is; S3's secret key, B2's
    key, Azure's key/SAS/secret, GCS/GDrive's service-account JSON, SFTP/WebDAV passwords, rclone's
    embedded config). Every returned config therefore goes through `repos.scrub_storage_config()` -
    the scrub happens ON READ, so a credential can never reach a response or `repos.json`."""
    data = read_config_file(config_file)
    storage = data.get("storage") or {}
    api = data.get("apiServer") or {}
    if api:
        # repo/api_server_repository.go APIServerInfo: `url` + `serverCertFingerprint`
        return {"type": "server", "kind": "server", "label": "Kopia repository server",
                "config": repos.scrub_storage_config({
                    "url": api.get("url") or "",
                    "serverCertFingerprint": api.get("serverCertFingerprint") or ""})}
    stype = storage.get("type") or ""
    return {"type": stype, "kind": repos.storage_kind(stype),
            "label": stype or "unknown",
            "config": repos.scrub_storage_config(storage.get("config") or {})}


def detect_configs() -> list[dict]:
    """Every kopia connection config found on this machine - the zero-typing path into KopiaLens.

    Both folders are scanned: kopia's own (KopiaUI's `repository-*.config` files and the CLI
    default) and KopiaLens's data folder for connections WE created there. Filesystem only (no
    spawn), reading the config JSON directly and scrubbing every credential out of it (see
    `describe_config`); it marks the ones already in the store."""
    base = kopia_config_dir()
    ours = os.path.join(config.DATA_DIR, "configs")
    default_cfg = os.path.join(base, "repository.config")
    known: dict[str, str] = {}
    for rec in repos.all_records():
        if rec.get("kind", "kopia") != "kopia":
            continue
        # no config_file = kopia's own default connection
        path = rec.get("config_file") or default_cfg
        known[os.path.normcase(os.path.abspath(path))] = str(rec.get("id"))

    out: list[dict] = []
    for folder in (base, ours):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".config"):
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            info = describe_config(path)
            key = os.path.normcase(os.path.abspath(path))
            out.append({
                "config_file": path,
                "filename": name,
                "folder": "kopia" if os.path.normcase(folder) == os.path.normcase(base) else "data",
                "is_default": key == os.path.normcase(os.path.abspath(default_cfg)),
                "managed": is_managed_config(path),
                "type": info["type"], "kind": info["kind"], "label": info["label"],
                "config": info["config"],
                "in_store": bool(known.get(key)),
                "repo_id": known.get(key, ""),
            })
    return out


def probe(config_file: str | None = None, repo: str | None = None) -> dict:
    """LIVE validation of one connection: `repository status --json` (an explicit user action -
    never on a polled request). Returns reachability, the unique id, storage type and capacity.

    A repository-SERVER connection reports no storage through this command (kopia's status JSON
    only fills storage/volume for a direct connection), so the type falls back to what the config
    file itself says."""
    if repo:
        if not repos.exists(repo):
            raise ValueError(f"unknown repo: {repo}")
        config_file = repos.config_path_for(repo)
    args = [kopiabin.exe()]
    if config_file:
        if not os.path.isfile(config_file):
            raise ValueError(f"config file not found: {config_file}")
        args += ["--config-file", config_file]
    args += ["repository", "status", "--json"]
    res = _run(args, None, timeout=120.0)
    out = {"ok": False, "config_file": config_file or "", "error": "", "type": "", "kind": "",
           "unique_id": "", "capacity": None, "available": None, "config": {}}
    if res["returncode"] != 0:
        out["error"] = _last_meaningful_line((res.get("stderr") or "") + "\n" + (res.get("stdout") or ""))
        out["error"] = out["error"] or f"kopia exited {res['returncode']}"
        return out
    try:
        data = json.loads(res["stdout"]) if (res["stdout"] or "").strip() else {}
    except ValueError:
        out["error"] = "kopia did not return a readable repository status"
        return out
    storage = (data or {}).get("storage") or {}
    volume = (data or {}).get("volume") or {}
    stype = storage.get("type") or ""
    config_facts = {}
    if not stype and config_file:
        config_facts = describe_config(config_file)
        stype = config_facts.get("type") or ""
    out.update({
        "ok": True, "unique_id": (data or {}).get("uniqueIDHex") or "", "type": stype,
        "kind": repos.storage_kind(stype),
        "config": repos.scrub_storage_config(storage.get("config")
                                             or config_facts.get("config") or {}),
        "capacity": volume.get("capacity"), "available": volume.get("available"),
        "error": "",
    })
    if repo:
        repos.capture_status(repo, data)
    return out


def summary(repo: str) -> dict:
    """What a connected repository holds, from one snapshot listing (an explicit user action):
    source, computer and snapshot counts, and when the newest snapshot finished."""
    from .sources import ListingFailed, _key_of, all_snapshots
    if not repos.exists(repo):
        raise ValueError(f"unknown repo: {repo}")
    try:
        snaps = all_snapshots(repo)
    except ListingFailed as exc:
        raise ValueError(str(exc)) from exc
    srcs = [s.get("source") or {} for s in snaps]
    return {
        "repo": repo,
        "snapshots": len(snaps),
        "sources": len({_key_of(s) for s in srcs}),
        "hosts": len({s.get("host") for s in srcs if s.get("host")}),
        "newest": max((s.get("endTime") or s.get("startTime") or "" for s in snaps), default=""),
    }


def attach(config_file: str | None, name: str = "", short_name: str = "",
           counts_as_backup: bool = True) -> dict:
    """Record an EXISTING kopia connection (KopiaUI's, the CLI default, a copied config). The file
    is never copied or rewritten - see the module docstring for why."""
    cfg = (config_file or "").strip() or None
    if cfg:
        cfg = os.path.abspath(cfg)
        if not os.path.isfile(cfg):
            raise ValueError(f"config file not found: {cfg}")
        facts = describe_config(cfg)
        if not facts["type"]:
            raise ValueError("that file is not a kopia connection config (no storage or server "
                             "settings inside)")
    else:
        cfg = None   # kopia's own default connection (repository.config)
        facts = {"type": "", "label": "default"}
    for rec in repos.all_records():
        existing = rec.get("config_file")
        if cfg is None and not existing and rec.get("kind", "kopia") == "kopia":
            raise ValueError(f"the default kopia connection is already attached as repo "
                             f"{rec.get('id')}")
        if cfg and existing and os.path.normcase(os.path.abspath(existing)) == os.path.normcase(cfg):
            raise ValueError(f"that connection is already attached as repo {rec.get('id')}")
    with _CONNECT_LOCK:
        rid = repos.reserve_id()
    record = repos.add_record({
        "id": rid, "name": name or f"Repo {rid}", "short_name": short_name, "kind": "kopia",
        "config_file": cfg, "source": "attached", "counts_as_backup": bool(counts_as_backup),
        # Seed the type from the config file itself so the UI knows what it is before any kopia
        # call; a live status capture (below) can refine it with capacity.
        "storage": {"type": facts.get("type") or "", "config": facts.get("config") or {}},
    })
    repos.capture_status(rid)   # live status; an offline repo just stays uncaptured
    audit.audit("executed", "repo-attach", repo=rid,
                summary={"config": cfg or "(default)", "type": facts.get("type")})
    return record


def unlock(repo: str, password: str) -> dict:
    """Re-persist the repository password for an attached connection whose password is not
    remembered (`kopia repository connect from-config` with the SAME config file - verified
    against kopia 0.23.1: the read completes before the rewrite, and the password then lives in
    the OS keyring / `<config>.kopia-password` as kopia normally stores it)."""
    rec = next((r for r in repos.all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    if rec.get("kind", "kopia") != "kopia":
        raise ValueError(f"repo {repo} is not a kopia repository")
    if not (password or "").strip():
        raise ValueError("Enter the repository password.")
    cfg = rec.get("config_file")
    if not cfg:
        cfg = os.path.join(kopia_config_dir(), "repository.config")
    if not os.path.isfile(cfg):
        raise ValueError(f"config file not found: {cfg}")
    describe = describe_config(cfg)
    if describe["type"] == "server":
        url = (describe["config"] or {}).get("url") or ""
        if not url:
            raise ValueError("the config does not carry a server URL")
        args = [kopiabin.exe(), "--config-file", cfg, "repository", "connect", "server",
                "--url", url, "--no-check-for-updates"]
        fingerprint = (describe["config"] or {}).get("serverCertFingerprint") or ""
        if fingerprint:
            args += ["--server-cert-fingerprint", fingerprint]
    else:
        args = [kopiabin.exe(), "--config-file", cfg, "repository", "connect", "from-config",
                "--file", cfg, "--no-check-for-updates"]
    res = _run(args, password, timeout=300.0)
    ok = res["returncode"] == 0
    audit.audit("executed", "repo-unlock", repo=repo, summary={"ok": ok}, rc=res["returncode"])
    if not ok:
        raise ValueError(_last_meaningful_line(res.get("stderr") or "") or
                         "kopia could not open the repository with that password.")
    status = probe(repo=repo)
    return {"ok": True, "status": status}


def remove_preview(repo: str) -> dict:
    """What removing this repo from KopiaLens would delete: the record plus every app-owned file
    derived from its id. The kopia repository itself is never touched."""
    rec = next((r for r in repos.all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    files, total = [], 0
    for path in repos.data_paths(repo):
        size = _size_of(path)
        total += size
        files.append({"path": path, "bytes": size})
    managed = is_managed_config(rec.get("config_file"))
    # Deleted only when the user ticks "delete the connection file too" - listed apart for that.
    config_files = [{"path": p, "bytes": _size_of(p)}
                    for p in (_config_siblings(rec["config_file"]) if managed else [])]
    return {"repo": repo, "name": rec.get("name") or repo, "paths": [f["path"] for f in files],
            "bytes": total, "files": files,
            "config_file": rec.get("config_file"), "managed_config": managed,
            "config_files": config_files,
            "kopia_repository_untouched": True}


def _size_of(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _disconnect(repo: str, config_file: str) -> None:
    """``kopia repository disconnect`` for a connection KopiaLens itself made: kopia then deletes
    the password it saved in the OS keychain and the connection's cache, which removing the files
    alone would leave behind. Local only, so it works with the storage offline."""
    if not os.path.isfile(config_file):
        return
    res = _run([kopiabin.exe(), "--config-file", config_file, "repository", "disconnect"], None,
               timeout=60)
    if res["returncode"] != 0:
        log.warning("repo=%s remove: kopia disconnect failed: %s", repo,
                    _last_meaningful_line(res.get("stderr") or ""))


def remove(repo: str, delete_config: bool = False) -> dict:
    """Remove a repo from KopiaLens: close its index generation, delete the app's own derived
    files, drop the record. The kopia repository and any config KopiaLens did not create are left
    alone. Returns what was deleted and what could not be (an open handle on Windows)."""
    from ..index import store   # lazy: repoconnect is importable before the store exists
    rec = next((r for r in repos.all_records() if str(r.get("id")) == str(repo)), None)
    if rec is None:
        raise ValueError(f"unknown repo: {repo}")
    from . import nightly
    if nightly.is_running(repo):
        raise ValueError(f"repo {repo} is being indexed right now - wait for the pass to finish")
    closed = store.close_repo(repo)
    # The record goes FIRST and must be saved: a failed save must not leave a record whose data
    # is already gone (it would re-index from scratch on the next start).
    try:
        repos.remove_record(repo)
    except OSError as exc:
        raise ValueError(f"could not save the repository list ({exc}) - nothing was removed") \
            from exc
    removed, failed = [], []
    for path in repos.data_paths(repo):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed.append(path)
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})
            log.warning("repo=%s remove: could not delete %s: %s", repo, path, exc)
    state_cleaned = repos.forget_state(repo)
    config_deleted = ""
    if delete_config and is_managed_config(rec.get("config_file")):
        cfg = rec["config_file"]
        if rec.get("source") in ("created", "connected"):
            _disconnect(repo, cfg)
        _remove_config_files(cfg)
        config_deleted = cfg
    audit.audit("executed", "repo-remove", repo=repo,
                summary={"name": rec.get("name"), "removed": len(removed), "failed": len(failed),
                         "config_deleted": config_deleted, "state_cleaned": state_cleaned,
                         "generations_closed": closed.get("closed"),
                         "generations_busy": closed.get("busy")})
    return {"ok": True, "repo": repo, "removed": removed, "failed": failed,
            "state_cleaned": state_cleaned,
            "config_deleted": config_deleted, "generations": closed}

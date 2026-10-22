# API

KopiaLens has an HTTP API with JSON replies. The KopiaLens screens use it, and so can scripts, the [command line](cli.md) and AI agents. This page covers what you need to call it safely.

## Address and sign-in

KopiaLens answers on `http://127.0.0.1:7123` by default (`https://` when HTTPS is on, see [Remote access and HTTPS](remote-access.md)). Call it with an [API token](api-tokens.md) in the `Authorization` header:

```
curl -H "Authorization: Bearer kl_..." http://127.0.0.1:7123/api/repos
```

A token works whether sign-in is on or off. When KopiaLens is set to open only in its own window, tokens work from this computer only.

## Version

`GET /health` needs no token and answers with the KopiaLens version and the API version:

```
{"ok": true, "version": "0.29.0", "api": 1}
```

The API version goes up only when a stable route below changes in a way that breaks a caller.

## Stable routes

These routes keep their paths, parameters and reply fields within one API version. New fields can appear in a reply, so ignore fields you don't know. Every other route belongs to the KopiaLens screens and can change between releases.

| Route | What it does |
|---|---|
| `GET /health` | Version and API version |
| `GET /api/repos` | The connected repositories, with their numbers |
| `GET /api/sources?repo=` | A repository's backed-up folders |
| `GET /api/search?repo=&q=` | Search files in the backups (`largest`, `ext`, `source`, `over_gb`, `deleted_only`, `limit`, `offset`) |
| `GET /api/search/detail?repo=&source=&full_path=` | One file: sizes, versions, what it shares, what removing it frees |
| `GET /api/folder/summary?repo=&prefix=&source=` | A folder's totals |
| `GET /api/folder/contents?repo=&prefix=&source=` | A folder's contents |
| `GET /api/snapshots?repo=` | A repository's snapshots |
| `GET /api/index/status?repo=` | Index state and freshness |
| `POST /api/index/build?repo=` | Start an index run |
| `GET /api/engine/status` | Backups, running tasks, and approval requests waiting |
| `POST /api/engine/snapshot` | Take a snapshot now: `{"repo", "source"}` |
| `POST /api/actions/remove-plan` | Preview removing files. Replies with a `plan_token` |
| `POST /api/jobs/start` | Start a job: a dry run, removing files, or deleting snapshots (below) |
| `GET /api/jobs/status?job_id=&after=` | A job's state and its new output lines |
| `GET /api/jobs/list` | Recent jobs |
| `POST /api/jobs/stop` | Stop a job: `{"job_id"}` |
| `GET /api/logs/app` | The KopiaLens log |
| `GET /api/audit` | The audit log |
| `GET /api/approvals/{id}` | An approval request this token made |

For the full list of routes and their fields, open `/docs` in KopiaLens while signed in.

## Removing files safely

Removing files from the backups is always three steps, and the last one waits for the user's approval:

1. **Plan.** `POST /api/actions/remove-plan` with the repository and a filter (`prefix`, `query`, `ext`, `source`). The reply lists the files, the sizes, and anything else that goes with them (`filename_extra_count`, `content_extra_count`). Keep its `plan_token`. It's valid for one hour.
2. **Dry run.** `POST /api/jobs/start` with `{"kind": "remove_files", "repo": "1", "params": {"form": "plan", "plan_token": "...", "dry": true}}`. Kopia reports what it would remove and changes nothing.
3. **Run.** The same request with `"dry": false`. It removes exactly the planned files, even if the index changed since the plan. It needs a Full Access token and the user's approval.

Deleting snapshots works the same way, with `{"kind": "snapshot_delete", "params": {"snapshot_ids": [...], "dry": true}}`.

Follow a job with `GET /api/jobs/status?job_id=...&after=<cursor>` until its `state` is no longer `running`. It ends as `done`, `failed` or `stopped`.

## When a request is refused

A refusal has a status code and a `detail` that says why. A refusal caused by the token also has the header `X-KopiaLens-Needs`:

| `X-KopiaLens-Needs` | Meaning |
|---|---|
| `approval` | The user has to approve this in KopiaLens (below) |
| `mode` | The token's permission is too low |
| `never` | A token can never do this (passwords, the recovery key, tokens, approvals) |

Other answers to expect:

| Status | Meaning |
|---|---|
| 401 | The token is missing, wrong, revoked or expired |
| 403 | Refused: see `detail`, and the repository's [write mode](settings.md#permissions) |
| 409 | Can't be done right now, for example a job of that kind is already running |
| 423 | The index is encrypted and locked after a restart. Ask the user to unlock it in KopiaLens. A token can't |
| 503 | The repository is being indexed, or has no index yet |

## Approvals

The unsafe side of an action, such as deleting data or turning sign-in off, runs only after the user approves it in KopiaLens:

1. Your request is refused with `X-KopiaLens-Needs: approval` and an `X-KopiaLens-Approval: <id>` header. KopiaLens shows the request to the user, with a tray notice.
2. Check `GET /api/approvals/<id>` until `status` is `approved`, `denied` or `expired`.
3. Once approved, send exactly the same request again, with the header `X-KopiaLens-Approval: <id>`.

An approval works once, only for the token that asked, and only for the identical request (same route, body and parameters). It expires after one hour. Waiting requests don't survive a KopiaLens restart, so ask again then. The [command line](cli.md) does all of this for you.

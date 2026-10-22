# Command line

`klens` (also installed as `kopialens-cli`) runs KopiaLens from a terminal, for scripts and AI agents. It talks to a running KopiaLens through its [API](api.md), with an [API token](api-tokens.md).

## Where to find it

- **Windows installer:** keep **Add the klens command line to PATH** ticked during setup (it's on by default). Then `klens` works in any new terminal. To install without it, run the installer with `/NOPATH`.
- **Portable copy:** run `bin\klens.exe` from the KopiaLens folder.
- **Docker:** `docker exec <container> klens status`.
- **Any other computer with Python 3:** download `klens.py` from the release and run `python klens.py --url http://<kopialens-address>:7123 status`.

## Sign in once

Create a token in **Settings → API tokens**, then:

```
klens login
```

Paste the token when asked. `klens` checks it and keeps it in Windows Credential Manager (the Keychain on macOS, a private file in your user folder on Linux). `klens logout` forgets it.

Instead of logging in, a script can set `KOPIALENS_TOKEN`, or pass `--token`. To reach another KopiaLens, use `--url` or set `KOPIALENS_URL`. `klens --url ... login` remembers that address. Options like `--url`, `--token` and `--table` go before the command name.

## Commands

| Command | What it does |
|---|---|
| `klens status` | KopiaLens version, repositories and backups |
| `klens repos` | The connected repositories and their numbers |
| `klens sources 1` | The backed-up folders in repository 1 |
| `klens search 1 "*.iso" --largest` | Search the files in the backups |
| `klens file 1 <source> <path>` | One file: sizes, versions, and what removing it frees |
| `klens folder 1 <path>` / `klens ls 1 <path>` | A folder's totals, or its contents |
| `klens snapshots 1` | A repository's snapshots |
| `klens index 1` | Index status. Add `--build` to start an index run |
| `klens snapshot 1 <source>` | Take a snapshot now |
| `klens plan 1 --prefix <path>` | Preview removing files. Prints a plan id |
| `klens dry-run 1 <plan>` | Ask kopia what the plan would remove |
| `klens run 1 <plan> --yes` | Remove the plan's files |
| `klens delete-snapshots 1 <id>... --dry-run` | Preview deleting snapshots. `--yes` deletes them |
| `klens jobs` / `klens job <id> --wait` | Recent jobs, or follow one to its end |
| `klens logs --level warning` | The KopiaLens log |
| `klens approval <id>` | The status of an approval request |
| `klens docs` / `klens docs cli` | The list of docs pages, or one page |
| `klens api GET /api/repos` | Call any API route (`--data` for a JSON body, `--query key=value`) |

Run `klens <command> --help` for every option. Replies are JSON. `klens --table jobs` prints a table instead. Progress and waiting messages go to the error output, so the JSON stays clean.

## Removing files

Removing is always plan, dry run, then run:

```
klens plan 1 --prefix "D:/Old projects"
klens dry-run 1 <plan>
klens run 1 <plan> --yes
```

The plan lists the files and sizes, and is valid for one hour. `run` removes exactly what the plan showed. It needs a Full Access token and your approval in KopiaLens.

## Approvals

When a command needs your approval, `klens` says so and waits:

```
Waiting for approval in KopiaLens (request 4f2a91c0). Approve or deny it in Settings > API tokens.
```

Once you approve it, the command carries on by itself. With `--no-wait`, it prints the request id and stops. Run the same command again with `--approval <id>` after approving.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | Failed, or the job ended as failed or stopped |
| 2 | Wrong use, for example `run` without `--yes` |
| 3 | Refused or denied: the token's permission, a step a token can never do, or a denied approval |
| 4 | Left waiting for approval (`--no-wait`) |

## HTTPS

`klens` accepts KopiaLens's own certificate on this computer. For another address with a self-signed certificate, add `--insecure`.

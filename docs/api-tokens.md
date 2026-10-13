# API tokens

An API token lets a script, the command line or an AI agent use KopiaLens without your password. Each token has a name, a permission and an expiry date, and you can revoke it at any time.

## Create a token

Open **Settings → API tokens**, give the token a name (for example `Claude on this PC`), pick a permission and an expiry, then select **Create token**. Copy the token straight away: KopiaLens shows it only once and keeps only a fingerprint of it.

While sign-in is on, a token above **Read-only** asks for your KopiaLens password first.

## Permissions

A token's permission works like the [write modes](settings.md#permissions):

| Permission | What the token can do |
|---|---|
| Read-only | Browse, search, preview and plan. It changes nothing. |
| Restores allowed | Also restore to disk, and change KopiaLens's own settings |
| Non-destructive | Also take snapshots, edit policies and descriptions, and run maintenance |
| Full Access | The same as Non-destructive for now: deleting always needs you |

A repository's own write mode still applies, so the stricter of the two wins.

Some things always need you in KopiaLens, whatever the token's permission:

- deleting files, snapshots or sources;
- turning sign-in off, opening remote access, or lowering encryption;
- a looser write mode, removing a repository or a licence;
- restoring a settings backup, and creating or revoking tokens.

A token can still do the safe side of these: turn sign-in on, raise encryption, make a write mode stricter, or add a licence. Setting a password or showing the recovery key are never done with a token.

## Use a token

Send it in the `Authorization` header:

```
curl -H "Authorization: Bearer kl_..." http://127.0.0.1:7123/api/repos
```

When KopiaLens is set to open only in its own window (**Settings → Remote access**), tokens work from this computer only.

A request a token can't make is refused with the reason. The header `X-KopiaLens-Needs` says why: `person` (ask the user to do it in KopiaLens), `never` (a token can never do it) or `mode` (the token's permission is too low).

## Sign-in off

Without sign-in, anyone who can open KopiaLens can already do everything, so a token adds no protection. It still limits and labels what each script or agent does.

## Revoke or expire

Select **Revoke** next to a token. Anything using it stops working straight away. An expired token stops working on its expiry date; create a new one to continue.

Every action a token takes is recorded in the audit log as `token:<name>`. Tokens aren't part of [settings backups](settings.md#config), so restoring one never brings back a revoked token.

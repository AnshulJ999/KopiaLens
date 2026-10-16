# API tokens

An API token lets a script, the command line or an AI agent use KopiaLens without your password. Each token has a name, a permission and an expiry date, and you can revoke it at any time.

## Create a token

Open **Settings → API tokens**, give the token a name (for example `Claude on this PC`), pick a permission and an expiry, then select **Create token**. Copy the token straight away: KopiaLens shows it only once and keeps only a fingerprint of it.

While sign-in is on, a token above **Read-only** asks for your KopiaLens password first.

## Permissions

A token's permission uses the same four levels as [**Settings → Permissions**](settings.md#permissions):

| Permission | What the token can do |
|---|---|
| Read-only | Browse, search, preview and plan. It changes nothing. |
| Restores allowed | Also restore to disk, and change KopiaLens's own settings |
| Non-destructive | Also take snapshots, edit policies and descriptions, and run maintenance |
| Full Access | Also ask to delete files and snapshots, each one after you approve it |

A repository's own write mode still applies, so the stricter of the two wins.

## Approvals

Some things wait for your approval in KopiaLens, whatever the token's permission:

- deleting files, snapshots or sources
- turning sign-in off, opening remote access, or lowering encryption
- a looser write mode, removing a repository or a licence
- lowering retention in a policy, or restoring policies from a backup
- pausing backups, and restoring a settings backup
- installing an update
- making a repository's connection read-only when another app shares it

A token can still do the safe side of these on its own: turn sign-in on, raise encryption, make a write mode stricter, add a licence, or raise retention.

When a token asks for one of these, KopiaLens shows the request under **Settings → API tokens → Approval requests**, with a notice at the top of the page and in the tray. Select **Approve…** to see exactly what it does, or **Deny**. An approval works once, for that exact request, and expires after one hour. If the action normally asks for your password, approving it asks for the password too.

A token can only ask for what its permission covers, so a Non-destructive token can't ask to delete. A few things are never done with a token, even with approval: setting a password, showing the recovery key, exporting accounts or keys, managing tokens, and approving requests.

## Use a token

Send it in the `Authorization` header:

```
curl -H "Authorization: Bearer kl_..." http://127.0.0.1:7123/api/repos
```

When KopiaLens is set to open only in its own window (**Settings → Remote access**), tokens work from this computer only.

A request a token can't make is refused with the reason. The header `X-KopiaLens-Needs` says why: `approval` (waiting for you to approve it), `never` (a token can never do it) or `mode` (the token's permission is too low). [API](api.md) has the details for scripts, and [Command line](cli.md) covers `klens`.

## Sign-in off

Without sign-in, anyone who can open KopiaLens can already do everything, so a token adds no protection. It still limits and labels what each script or agent does.

## Edit a token

Select **Edit** next to a token to change its name, permission or expiry. The token itself stays the same, so anything using it keeps working. While sign-in is on, raising the permission or extending the expiry asks for your password. To hand a token to something you trust less, revoke it and create a new one instead.

## Revoke or expire

Select **Revoke** next to a token. Anything using it stops working straight away. An expired token stops working on its expiry date. Create a new one to continue.

Every action a token takes is recorded in the audit log as `token:<name>`. Tokens aren't part of [settings backups](settings.md#config), so restoring one never brings back a revoked token.

# Free and Pro

Everything KopiaLens shows you is free: sizes, duplicates, reclaimable space, churn, search, snapshots, policies, restore and every preview. **Pro** adds the actions that remove data from inside your backups:

- removing files from snapshots (the file panel, folder and filter removals, their dry runs, commands and scripts);
- deleting every snapshot that holds a file;
- smart exclude rules (same name, same extension or similar names anywhere);
- exclude & delete.

Deleting snapshots you pick yourself, running maintenance, and excluding a single file or folder stay free.

In the free version, Pro buttons carry a **PRO** tag. Selecting one explains what Pro adds; the preview beside it stays complete, so you can see exactly what a Pro action would free before deciding.

Demo repositories and sample data unlock everything, because nothing in them is real: try any Pro action there.

## Add your licence

Open **Settings → Licence**.

- **Licence key**: paste the key from your purchase email and select **Activate**. KopiaLens contacts the licence service once, then works offline.
- **Import licence file…**: for computers without internet. On another device, open the licence page shown on this screen, enter your key, download the licence file and import it here.
- **Sign in with Patreon**: your browser opens Patreon. Once you've signed in, Pro turns on while you're a patron.

The licence shows the plan, who it's licensed to and when it ends.

When KopiaLens contacts the licence service (activating, renewing, Patreon sign-in), it also sends a random install number kept in its licence folder, so we can see how many computers use each licence. It isn't the usage-stats ID, and it isn't sent from a Docker container whose data folder isn't on a volume.

## Renewals

Subscription and Patreon licences renew on their own a few days before they end, when the computer is online. If renewal isn't possible for a while (no internet, or the service is down), Pro keeps working for 7 more days after the end date. If KopiaLens wasn't running then, it renews the next time it starts, as long as the subscription or Patreon membership is still active. **Settings → Licence → Renew now** tries straight away and shows why when it can't.

A subscription added from a licence file renews the same way once the computer is online. A computer that never goes online needs a new licence file after each yearly payment.

When a licence ends, KopiaLens keeps working as the free version; nothing is locked or lost. Licences that are refunded stop working once KopiaLens next checks its list of withdrawn licences.

## Docker and servers

Set the licence in the environment instead of the screen:

- `KOPIALENS_LICENSE`: the licence text itself.
- `KOPIALENS_LICENSE_FILE`: the path of a licence file inside the container.

A licence set this way shows in **Settings → Licence**, but can only be changed where it's set. A subscription or Patreon licence set this way still renews; the renewed copy is kept in KopiaLens's data folder.

## Remove a licence

**Settings → Licence → Remove licence…** removes it from this computer. KopiaLens then runs as the free version until you add it again. A licence activated with a key also frees its place, so the key can be activated on another computer.

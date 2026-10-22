# Exclude a folder and remove it from old snapshots

You've found a folder that never needed backing up, such as a cache, a downloads folder or a build folder. This guide keeps it out of future snapshots and removes it from the old ones, in one step. **Exclude & delete** is a [Pro](licensing.md) action. Excluding alone is free.

## 1. Find the folder

Find it in the Tree, Search or Churn. Open its **Details** to check:

- **Reclaimable**: what removing it gives back.
- **Backed up by**: which sources hold it. With several, the delete covers each one you include.

## 2. Exclude it and delete it

1. Right-click the folder and select **Exclude & delete…**.
2. Choose what the rule matches. **This item** excludes just this folder. **Same name** excludes every folder with this name, for example every `node_modules` ([Exclude](exclude.md)).
3. Leave **Where the rule goes** on **Smart**, unless you have a reason to choose the policies yourself.
4. Check **Leaves out** and **Changes**, then select **Next: delete**.
5. Check the list of files to delete. If a source still backs the folder up, the panel says so.
6. Select **Dry run the delete**. Kopia checks the delete without changing anything.
7. Select **Exclude and delete…** and confirm.

KopiaLens first adds the rule, with a policy backup before each change. Then it removes the folder from the snapshots. If the rule can't be added, the delete doesn't run.

## 3. Check the result

- The [job panel](job-panel.md) shows the rules it saved and what the delete freed.
- KopiaLens updates its index a few minutes later, and the folder disappears from the Tree and Search.
- The next snapshot of the source leaves the folder out. The **Policies** tab lists the new rule.

The space comes back after kopia's next full maintenance. Select **Run maintenance…** in the job panel to start one now ([Maintenance](maintenance.md)).

## Exclude only

To keep the folder in your old snapshots and only stop backing it up from now on, select **Exclude…** instead of **Exclude & delete…**. That's free.

## Related

- [Exclude](exclude.md)
- [Delete from backups](delete-files.md)
- [Policies](policies.md)

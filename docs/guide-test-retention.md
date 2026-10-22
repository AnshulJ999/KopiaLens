# Test a retention policy before using it

Changing [retention](glossary.md#retention) is the cleanest way to free space, but a mistake deletes restore points you can't get back. This guide tries a policy first, sees exactly what it would keep and free, and only then applies it.

## 1. See what you keep today

1. Open the **Snapshots** tab and pick a source, or **All sources**.
2. Look at the **Retention** column. It shows why kopia keeps each snapshot, for example `daily-3` or `monthly-2`. A snapshot with no reason and no pin goes the next time kopia backs up that source.

## 2. Try a new policy

1. Select **Simulate retention…**.
2. Enter the numbers to keep: **Latest**, **Hourly**, **Daily**, **Weekly**, **Monthly**, **Annual**.
3. Select **Run**.

Simulate shows, per source, how many snapshots the policy keeps and deletes, and what that frees. Nothing changes in the repository ([Simulate](simulate.md)).

Try a few versions. For example, compare keeping 12 monthly snapshots with keeping 24.

## 3. Check the total

With **All sources**, the line above the table gives the total. It's less than the sources' figures added up when sources share data, because shared data comes back only when every source deletes it.

## 4. Protect what must stay

Before you apply a tighter policy, pin any snapshot you want to keep whatever happens, for example the last one before a big change:

1. Select it in the **Snapshots** tab.
2. Right-click and select **Pin…**.

Retention never removes a pinned snapshot.

## 5. Apply it

1. Open the **Policies** tab and select the policy: **Global policy** for every source, or the source's own.
2. Open **Retention** and enter the same numbers.
3. Check **What the new retention keeps**: it counts the snapshots each source keeps and drops.
4. Select **Review and save…**, then **Save changes**.

Kopia deletes the dropped snapshots the next time it backs up each source. The space comes back after the next full maintenance ([When the space comes back](space.md#when-the-space-comes-back)).

## Related

- [Simulate](simulate.md)
- [Policy editor](policy-editor.md#retention)
- [Snapshots](snapshots.md)

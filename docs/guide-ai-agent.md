# Use KopiaLens with an AI agent

An AI agent, such as a coding assistant in your terminal, can use KopiaLens to answer questions about your backups and to prepare cleanups. This guide gives an agent safe access: it can look at everything, and anything that removes data waits for your approval.

## How agents reach KopiaLens

Agents use `klens`, KopiaLens's command line ([Command line](cli.md)). It talks to a running KopiaLens with an [API token](api-tokens.md). Every token has a permission, and KopiaLens checks it on the server, so an agent can't do more than its token allows.

## 1. Create a token for the agent

1. Open **Settings → API tokens**.
2. Create a token, and name it after the agent.
3. Choose its permission. Start with **Read-only**: the agent can look, search and plan, but changes nothing.
4. Copy the token. KopiaLens shows it once.

Give each agent its own token, so you can revoke one without affecting the others.

## 2. Give the agent the token

Let the agent run `klens` with the token in the `KOPIALENS_TOKEN` environment variable, or sign in once with `klens login` in the agent's terminal.

Then tell the agent about KopiaLens. `klens docs` lists these docs pages and `klens docs <page>` prints one, so the agent can read how KopiaLens works from the source. For example:

```
Use the klens command to look at my KopiaLens backups. Start with klens status, and read klens docs cli first.
```

## 3. Ask questions

An agent with a read-only token can answer questions like these:

- Which folders take the most space in repository 1?
- Which files changed the most in the last month?
- Is anything on my D: drive not backed up?
- What would removing my old downloads folder from the backups free?

## 4. Let it clean up, with your approval

To let an agent remove files, give it a token with **Full Access**. Removal still goes through the same steps as in the app: a plan, a dry run, then the run. The run waits for your approval:

1. The agent prepares a plan and runs the dry run.
2. When it runs the plan, `klens` asks for your approval and waits.
3. KopiaLens shows the request. Review what it removes, then approve or deny it ([Approvals](api-tokens.md#approvals)).

A token can never approve its own request, and an approval never lifts a token above its permission.

## Keep it safe

- Use the lowest permission that does the job. You can change a token's permission later.
- Set an end date on tokens you only need for a while.
- Revoke a token you no longer use ([Revoke or expire](api-tokens.md#revoke-or-expire)).
- Every action an agent takes appears in **Logs → Audit**, with the token's name.

## Related

- [Command line](cli.md)
- [API tokens](api-tokens.md)
- [API](api.md)

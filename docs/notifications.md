# Notifications

KopiaLens can tell you when something needs attention: a backup failed, maintenance failed, or another app started running a repository's backups. Messages come from three places, and you set up all of them in **Settings → Notifications**.

## In the app and the tray

The top setting picks what KopiaLens shows on screen:

- **All**: everything, including finished indexing and finished jobs.
- **Warnings and errors**: the default.
- **Off**: nothing pops up.

In the desktop app these appear as tray notifications. In a browser they appear inside the page while it's open.

## From KopiaLens: a webhook

KopiaLens can send its own messages to a web address. It covers what kopia doesn't report itself: failed KopiaLens jobs, failed backups and maintenance that KopiaLens recorded, KopiaLens's backup server stopping, and another app taking over a repository's backups.

1. Enter the **address**, for example an [ntfy](https://ntfy.sh) topic like `https://ntfy.sh/my-backups`.
2. Pick the **format**. Plain text suits ntfy (the title goes in ntfy's Title header). JSON suits services that read structured data.
3. Add **headers** if the service needs them, one per line, for example `Authorization: Bearer tk_…` for a protected ntfy topic.
4. Pick the **level**: errors only, warnings and errors, or everything.
5. **Save**, then send a test.

## From kopia: notification profiles

kopia itself can report on backups and maintenance. Its settings are stored **inside the repository**, so KopiaUI, the kopia command line and KopiaLens all see the same profiles. Choose the repository, then **Add profile…**:

- **Email**: an SMTP server, sign-in, and the sender and recipient addresses.
- **Pushover**: your app token and user key.
- **Webhook**: an address, POST or PUT, extra headers, and a format. For ntfy, use your topic address with POST and plain text.

Each profile has a minimum level, from errors only up to everything. **Save and send a test** saves the profile and sends a test message straight away. **Test** sends another one later. When you edit a profile, leave a password field empty to keep the saved one.

kopia only sends these while something runs that repository's backups or maintenance: KopiaUI, a kopia server, or KopiaLens when it runs the backups (see [Backups](backups.md)).

Note: kopia only accepts notification passwords on its command line, so while KopiaLens saves a profile, the password is briefly visible to other programs on this computer. KopiaLens never writes it to its logs.

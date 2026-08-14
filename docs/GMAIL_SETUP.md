# Gmail setup (M5)

Create a Google Cloud OAuth **Desktop/installed application** client, enable Gmail API, configure
the consent application, and keep both its client-secret JSON and the resulting token JSON outside
`archive.root`. Configure `config_ref: file:/absolute/token.json`, then run:

```bash
mailarchive gmail auth --account personal-gmail --config /etc/mailarchive/config.yaml
```

MailArchive requests only `https://www.googleapis.com/auth/gmail.readonly`; it refuses stored
Gmail write-capable scopes. The token is written atomically with mode 0600 after profile email
verification. Google OAuth testing/publishing settings can limit long-running refresh tokens;
operators must choose and maintain an appropriate Google project publishing state.

M5 polls Gmail history locally every 90 seconds by default (60–120 seconds), inventories SPAM and
TRASH, and never uses Gmail IMAP, Pub/Sub, or a Gmail mailbox mutation endpoint.

## Optional destructive M12 credential

M12 remote deletion is separate from M5 acquisition. Keep `config_ref` as the `gmail.readonly`
token above. For a deliberately opted-in account, configure a different file outside `archive.root`:

```yaml
gmail:
  remote_delete_token_file: /etc/mailarchive/secrets/personal-gmail-delete-token.json
```

Create or refresh that credential with:

```bash
mailarchive gmail auth-delete --account ACCOUNT --config CONFIG
```

This command obtains only the separate `https://mail.google.com/` mutation credential, verifies
the authenticated profile email, and writes the mutation token atomically with mode 0600. The file
must not be a symlink and the command never widens, replaces, or reads the M5 readonly token for
destructive work. `remote_deletion_enabled: true` is a separate account opt-in and still does not
bypass retention, verified-backup, exact-plan, TTL, or current-identity checks. See
`REMOTE_DELETION_RUNBOOK.md` before any execute-plan operation.

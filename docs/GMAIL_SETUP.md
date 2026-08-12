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

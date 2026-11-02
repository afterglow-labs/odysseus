# Cybersecurity mode

Odysseus can switch to an Afterglow Cybersecurity workbench with the button at
the top of the web client. The two servers keep their own data and processes.
Odysseus calls a fixed set of Afterglow API routes from its backend; the web
client only calls Odysseus. Switching back restores the regular chat and agent
controls. The selected mode and task are kept in the browser's local storage.

## Run the two servers

Start Afterglow from its own checkout. For native Odysseus, set these values in
Odysseus's `.env` or process environment and start Odysseus normally:

```dotenv
AFTERGLOW_URL=http://127.0.0.1:8787
AFTERGLOW_PASSWORD=your-existing-afterglow-admin-password
```

An existing Afterglow JWT can be supplied as `AFTERGLOW_TOKEN` instead. The
token takes precedence over the password and must be replaced when it expires.
The password path logs in again when its cached JWT expires. Keep either
credential server-side. The web client never receives it.

`AFTERGLOW_URL` must be an HTTPS origin or a loopback HTTP origin. For two
containers connected to a trusted private Docker network, set
`AFTERGLOW_URL=http://scopeweaver:8787` and `AFTERGLOW_ALLOW_HTTP=true` in the
Odysseus environment. Both containers must be attached to that network; their
separate Compose projects do not share a network by default. Odysseus's
`docker-compose.yml` passes these variables into its container. Keep the
Afterglow port bound to loopback on the host and do not publish its private
Docker network. If the servers communicate across an untrusted network, use
HTTPS instead.

## Current workbench

- Overview: engine readiness, task and finding counts, pending interceptions,
  traffic, and recent records.
- Tasks: select a task, read its objective, and pause or resume running work.
- Findings: search and filter the paginated list; open evidence and reports as
  plain text so untrusted finding content cannot execute in Odysseus.
- Scope: read declared task scope and rough coverage. **This scope is a
  coverage and asset filter, not a network boundary.**
- Activity: inspect recent task events.
- Intercept: inspect pending tool requests and directly allow or deny them.
- Traffic: inspect the recent captured exchange list and capture status.

Task creation, task chat, report editing, interception rule configuration,
asset management, and other Afterglow administration still use Afterglow's own
interface. The Odysseus switch currently covers the operational views above;
it does not replace the full Afterglow UI yet. The iOS client is outside this
integration.

The [desktop](screenshots/cybersecurity-mode-desktop.png) and
[mobile](screenshots/cybersecurity-mode-mobile.png) previews show a disposable
fixture with invented task and finding data.

## Security boundary

The gateway is admin-only, uses fixed upstream paths, rejects redirects, caps
JSON responses, and keeps the Afterglow credential out of browser storage and
the global MCP server registry. Write routes require a custom request header.
The Afterglow service still decides how a task runs and how interception rules
apply. There is no extra confirmation step in Odysseus after a user clicks a
task control or interception decision.

At this stage Afterglow authenticates the gateway with its full-admin JWT or
password. Odysseus's ordinary agent can run local tools, so this credential
must be treated as accessible to a compromised Odysseus process or unrestricted
local shell. A scoped Afterglow service credential and stronger agent isolation
are needed before treating the gateway as a security boundary against hostile
prompt content. Keep `AUTH_ENABLED=true` and `LOCALHOST_BYPASS=false` for any
network-accessible Odysseus deployment.

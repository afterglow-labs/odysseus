# Cybersecurity mode

Odysseus can switch to an Afterglow Cybersecurity workbench with the button at
the top of the web client. The two servers keep their own data and processes.
Odysseus calls a fixed set of Afterglow API routes from its backend; the web
client only calls Odysseus. Switching back restores the regular chat and agent
controls. The selected mode and task are kept in the browser's local storage.

## Run the two servers

Start the combined `afterglow-labs/afterglow-cybersecurity` server from its own
checkout. That checkout supplies the full API used by this workbench, including
the ChatGPT account and model routes. The standalone `afterglow-labs/scopeweaver`
checkout currently lacks those ChatGPT routes and is not a complete backend for
this mode. Its Go module migration is separate from this integration.

For native Odysseus, set these values in Odysseus's `.env` or process
environment and start Odysseus normally:

```dotenv
AFTERGLOW_URL=http://127.0.0.1:8787
AFTERGLOW_PUBLIC_URL=http://127.0.0.1:8787
AFTERGLOW_TOKEN=your-afterglow-gateway-token
```

On a combined Afterglow backend with the gateway-token endpoint, send
`POST /api/auth/gateway-token` with an administrator JWT in the bearer header.
Copy the returned token into `AFTERGLOW_TOKEN`; it expires after 30 days and
must then be renewed. The token is limited to this workbench's backend routes.
The existing `AFTERGLOW_PASSWORD` login path remains available for older
backends, but gives Odysseus a full administrator credential. The token takes
precedence when both are set. Keep the credential server-side; the web client
never receives it.

`AFTERGLOW_URL` must be an HTTPS origin or a loopback HTTP origin. The combined
backend's Compose service is named `scopeweaver`. For two containers connected
to a trusted private Docker network, set `AFTERGLOW_URL=http://scopeweaver:8787`
and `AFTERGLOW_ALLOW_HTTP=true` in the Odysseus environment. Both containers
must be attached to that network; their separate Compose projects do not share
a network by default. Odysseus's `docker-compose.yml` passes these variables
into its container. Keep the
Afterglow port bound to loopback on the host and do not publish its private
Docker network. If the servers communicate across an untrusted network, use
HTTPS instead.

`AFTERGLOW_PUBLIC_URL` is the address that the user's browser can open. When
set, the Cybersecurity header offers a link to the full Afterglow console for
operations that are not yet native in Odysseus. It uses Afterglow's own login;
the gateway credential is never added to the link. Set this to an HTTPS origin
for remote browsers. Omit it if the browser cannot reach Afterglow directly.

## Local model

Run Ollama on the Afterglow host and install the model before creating a profile
in the Cybersecurity **Models** tab:

```sh
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 ollama serve
ollama pull qwen3:32b
ollama list
```

Run `ollama serve` as a service or in a separate terminal; the pull and list
commands use the same Ollama endpoint. Set these values on the **Afterglow**
server, not Odysseus:

```dotenv
SCOPEWEAVER_LOCAL_LLM_ONLY=true
SCOPEWEAVER_LLM_PROVIDER=ollama
SCOPEWEAVER_LLM_MODEL=qwen3:32b
SCOPEWEAVER_LLM_BASE_URL=http://127.0.0.1:11434
SCOPEWEAVER_LLM_CONTEXT_WINDOW_K=32
```

Use **Find installed models** in the Models tab to confirm that Afterglow can
reach Ollama. If Afterglow runs in a container, its Ollama endpoint must be
reachable from that container; `127.0.0.1` inside the container refers to the
container itself. Keep the Ollama listener private.

## Current workbench

- Overview: engine readiness, task and finding counts, pending interceptions,
  traffic, and recent records.
- Tasks: create and select tasks, choose an existing model profile, read the
  objective, pause or resume work, and add, edit, or delete goals and task
  constraints. Goal additions and edits wake the planner and can resume a task.
  Constraints enter agent prompts as guidance; they do not enforce network or
  tool limits.
- Learning: review task outcomes, evidence-backed lessons, the business map,
  campaign branches, hunt coverage, and attack chains. Operators can extract
  available lessons for a task; advanced board edits and chain verification
  use the full Afterglow console.
- Task chat: read the current main-agent conversation, send messages, stop a
  reply, or start a new conversation segment.
- Findings: search and filter the paginated list, triage status and severity,
  and open evidence and reports as plain text so untrusted finding content
  cannot execute in Odysseus.
- Scope: add or remove manual scope rows and read rough coverage. **This scope is a
  coverage and asset filter, not a network boundary.**
- Assets: list task assets and register root domains, subdomains, IP addresses,
  and HTTP service URLs. Credential fields are omitted from asset list responses.
- Activity: inspect recent events from the task's current main session.
- Intercept: inspect pending tool requests, directly allow or deny them, and
  create, enable, disable, or remove task asset rules.
- Traffic: inspect the recent captured exchange list and capture status.
- Reports: view and download Afterglow's generated Markdown task report.
- Models: inspect the active model, configured profiles, model failover state,
  and ChatGPT account status; discover installed models from fixed local Ollama
  endpoints, create and test a local profile, and activate an existing profile.
  Discovery does not download a model. On the Afterglow machine, connect or
  disconnect ChatGPT accounts, list their plan models, create plan profiles,
  and recheck saved profiles.

The ChatGPT connection uses Afterglow's loopback OAuth callback, so open the
Odysseus page in a browser on the same machine as both servers. A remote browser
can inspect account status but cannot start or revoke a connection through this
workbench. Afterglow's local-only LLM policy blocks ChatGPT inference and model
catalog access while it is enabled; the downloaded Ollama model remains the
configured local option. Other cloud model profiles, report editing, global
interception policy, advanced asset and task administration, and other Afterglow
settings use Afterglow's own interface. The iOS client is outside this
integration.

The [desktop](screenshots/cybersecurity-mode-desktop.png) and
[mobile](screenshots/cybersecurity-mode-mobile.png) previews show a disposable
fixture with invented task and finding data. The
[Learning desktop](screenshots/cybersecurity-learning-desktop.png) and
[Learning mobile](screenshots/cybersecurity-learning-mobile.png) previews also
use invented task data.

## Security boundary

The gateway is admin-only, uses fixed upstream paths and typed request bodies,
rejects redirects, caps JSON responses, and keeps the Afterglow credential out
of browser storage and the global MCP server registry. Write routes require a
custom request header and reject a different browser Origin.
The Afterglow service still decides how a task runs and how interception rules
apply. There is no extra confirmation step in Odysseus after a user clicks a
task control or interception decision.

The gateway token limits backend paths and methods, but still permits task
control and interception decisions. Odysseus removes its Afterglow token and
password from the environments it passes to direct agent shell, Python,
background job, ripgrep, and MCP subprocesses. Its ordinary agent can still
run local tools, so a compromised Odysseus process or unrestricted same-user
shell may reach server files or process state. Stronger agent isolation is
needed before treating the gateway as a security boundary against hostile
prompt content. Keep
`AUTH_ENABLED=true` and `LOCALHOST_BYPASS=false` for any network-accessible
Odysseus deployment.

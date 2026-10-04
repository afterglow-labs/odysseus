# Odysseus native iOS client handoff

Prepared for Corey on October 4, 2026. Build a native SwiftUI remote client for the existing Odysseus desktop server, using the existing interface as the visual and workflow guide. Corey explicitly selected a native client instead of a web view wrapper.

The first native client is implemented under `ios/`, with a checked-in Xcode project and shared scheme. It builds on Corey's Mac through SSH from WSL; a development-signed device archive and simulator builds have succeeded. Chat, Agent, model controls, personas, attachments, and connection/account screens are native SwiftUI. The remaining desktop workspaces still need native screens. Read [ios/README.md](ios/README.md) for reproducible build/test commands and current limits. The separate `swift/odysseus-mlx-image-bridge` package serves desktop MLX tools and is unrelated to this client.

## Continue on the Mac

Clone the actual working repository and its `dev` branch:

```bash
git clone --branch dev https://github.com/afterglow-labs/odysseus.git
cd odysseus
```

For an existing clean checkout of that repository, use `git switch dev` followed by `git pull --ff-only`. Preserve any local changes before updating. The accompanying source ZIP is an alternative snapshot without Git history; a clone is preferable for continued development.

Open this directory in Codex on the Mac and give it this instruction:

> Read IOS_HANDOFF.md, ios/README.md, and CONTRIBUTING.md, then continue the native SwiftUI iOS client under ios/. Use the existing Odysseus interface as the guide. The iPhone is a remote client for my existing Ubuntu WSL2 Odysseus server. Preserve the implemented connection, chat, controls, personas, and attachment workflows and the existing desktop fixes. Verify the actual phone connection, permissions, image/video inference, and Thinking Off with the intended WSL server. Continue the remaining desktop workflows as native screens in stages and report any remaining gaps honestly.

The client targets iOS 17+. The Mac has Xcode 27 beta and iOS 27.0 and 18.5 simulator runtimes. WSL's `ssh afterglow-mac` now uses a dedicated local key; the private key stays outside the repository. The isolated Mac build copy is `/Users/coreyhamilton/Projects/odysseus-ios-build/ios`; the existing Mac Odysseus checkout was preserved. Automatic signing uses the Mac's existing Apple development identity. Signing over SSH needed execution in the logged-in GUI session because background SSH could not access the login Keychain. No signing keys or passwords were exported.

The phone still needs a reachable desktop URL and installation onto a provisioned device. Tailscale Serve was disabled on the tailnet at build time; its enablement prompt was sent to Corey. Do not treat a simulator fixture test or a signed archive as proof of a live phone connection.

Build verification: 14 protocol/API tests and 3 native UI tests passed on iOS 18.5; core chat and controls flows also passed on iOS 27.0, and the controls/chat/stop flow passed on an iPad simulator. A development IPA was exported using the Mac's existing profile. Artifacts are kept outside Git; see the build scripts in `ios/scripts/` to reproduce them. Photo-library permissions, physical-device installation, and native-to-live-server image/video inference remain acceptance work.

## Product and interface

The phone owns presentation, connection state, uploads, and a local display cache. The desktop owns model execution, Python packages, GPU memory, model downloads, credentials for model providers, tools, and persistent conversations. The client talks to the Odysseus application API, not directly to llama.cpp or another model provider.

Use native navigation, sheets, pickers, document/photo import, text selection, sharing, and keyboard handling. Adapt the desktop layout for iPhone and iPad while preserving its recognizable appearance and terminology. The source palette is in `static/style.css`: background `#282c34`, foreground `#9cdef2`, panels `#111`, borders `#355a66`, and coral `#e06c75`. Read the active theme variables rather than treating these as the whole theme system. `CONTRIBUTING.md` specifies Fira Code for primary UI text, monochrome icons, and a dark default; preserve that character with accessible Dynamic Type sizing.

The existing interface is defined by `static/index.html`, `static/style.css`, and `static/js/`. It includes New Chat, Search, Brain, Calendar, Compare, Cookbook, Deep Research, Gallery, Library, Notes, Tasks, and Theme. The composer has model selection, model controls, Chat/Agent mode, attachments, and additional tools. Use these as the roadmap; a finished chat screen alone is not full feature parity.

Suggested delivery sequence:

1. A working connection and authenticated native conversation flow, including history, streaming, stop, and reconnection.
2. Model controls, native attachments, agent activity and approval cards, and persona editing/selection.
3. The remaining desktop workflows as native screens, prioritizing Cookbook status and controls, Notes, Tasks, Calendar, Library, and Gallery. Inventory their routes before implementation; do not invent API contracts or ship inert navigation items.

Show the selected server address and account in connection settings and make connection status discoverable from chat. Corey has repeatedly been routed to a different Windows installation by mistake; connection identity must be easy to verify. Do not infer server operating system from the client device or a friendly name entered by the user.

## Desktop environment

The active development checkout is `/home/corey/projects/odysseus` inside Ubuntu WSL2. Its private `venv` uses CPython 3.14.6. Do not install into global Python or modify the separate Windows Odysseus installation.

The local Odysseus application is configured on `127.0.0.1:7860`. The desktop model server has been running at `localhost:8001/v1`; that address is meaningful to the desktop. On an iPhone or Mac, `localhost` refers to that device, not the WSL machine.

Existing desktop commands are `Odysseus` and `odysseus` for the client window, `odysseus-start` for the application server, and `odysseus-stop` to stop the application server. They are machine-local launchers. Stopping the application is separate from stopping Cookbook model jobs.

The local launcher `dist/Odysseus`, private Python environment, CUDA llama-server, FFmpeg/FFprobe, downloaded models, and personal profile are ignored runtime/data artifacts. They are **not** included in Git or the source ZIP. The existing server keeps using them; an iOS client does not need copies of those runtimes or the Windows profile archive.

The recent working native model was a Qwen3.5 9B Q8 GGUF with a separate `mmproj-F32.gguf` projector. The private llama.cpp build is b11382, commit `11fe02151`, with CUDA 12.8. Its `/props` reported both vision and video support. These are observations of the configured desktop, not client defaults to hardcode.

## Connecting from the phone

Accept a user-configurable Odysseus application base URL and keep it distinct from model endpoint URLs returned by the API. Prefer an authenticated HTTPS address reachable from the Mac and phone. A private HTTPS proxy or VPN is one deployment option; network exposure has not been configured by this handoff.

WSL access from Windows through localhost does not by itself establish access from another device. Verify the actual network mode, listener, forwarding/proxy, and firewall before diagnosing a phone connection as an app failure. Do not assume a WSL private IP is stable. See [Microsoft's WSL networking documentation](https://learn.microsoft.com/en-us/windows/wsl/networking).

For local network access, add a clear `NSLocalNetworkUsageDescription` and test denial as well as acceptance on a device. Configure ATS deliberately for the supported local connection forms and deployment target; do not globally disable certificate validation. See [Apple's local network privacy guidance](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy) and [NSAllowsLocalNetworking](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking).

Use a dedicated URLSession/cookie context for each connection. Persist authentication securely, scope it to the server and account, and clear it on logout or server change. Keep provider API keys and Hugging Face credentials on the desktop. Verify that redirects cannot forward credentials to a different host.

## API contracts already inspected

The route implementations and existing JavaScript are the source of truth. Do not rely only on `src/request_models.py`: several live endpoints accept form fields even where a similarly named JSON request model exists.

| Operation | API and details | Source |
| --- | --- | --- |
| Sign in | `POST /api/auth/login`, JSON `username`, `password`, optional `totp_code`, and `remember`. Success sets the `odysseus_session` cookie. A successful password check can instead return `requires_totp: true`; finish 2FA before entering the app. | `routes/auth_routes.py` |
| Account state | `GET /api/auth/status`; inspect authenticated state and returned privileges. `POST /api/auth/logout` revokes the session and deletes its cookie. | `routes/auth_routes.py`, `core/auth.py` |
| List conversations | `GET /api/sessions` returns an array with IDs, names, model/endpoint identity, timestamps, mode, folder, and other metadata. | `routes/session_routes.py` |
| Create conversation | `POST /api/session`, form fields including `name`, `endpoint_id`, `endpoint_url`, `model`, `rag`, and optional `reasoning_effort`. Prefer a selected stored `endpoint_id`; the server resolves credentials. | `routes/session_routes.py` |
| Update conversation | `PATCH /api/session/{sid}`, form fields. A model change currently requires both `model` and `endpoint_url` to enter the update branch; also pass the selected `endpoint_id`. | `routes/session_routes.py` |
| History | `GET /api/history/{session_id}?limit=50`; optional `offset`. Returns `history`, session model/endpoint identity, and pagination metadata. The default page is the latest page; maximum page size is 100. Content may be text or structured content. | `routes/history/history_routes.py` |
| Model picker | `GET /api/models` returns `{hosts, items}`. Each item groups `endpoint_id`, `endpoint_name`, `url`, raw `models`, parallel `models_display`, extras, category, and possible `offline` state. Preserve raw IDs separately from display names. | `routes/model_routes.py` |
| Send message | `POST /api/chat_stream`, multipart form data, returning SSE. Match the browser form contract for complete Chat/Agent behavior; JSON fallback does not cover every form feature. | `routes/chat_routes.py`, `static/js/chat.js` |
| Resume stream | `GET /api/chat/resume/{session_id}` returns SSE for an active detached run, replaying from its beginning. HTTP 404 means no active run; reload persisted history. | `routes/chat_routes.py`, `src/agent_runs.py` |
| Stream state | `GET /api/chat/stream_status/{session_id}` reports the current stream or returns 404 when none is active. | `routes/chat_routes.py` |
| Stop generation | `POST /api/chat/stop/{session_id}` with the exact `X-Odysseus-Run-Id` from the stream response. Returns `{stopped}`. Missing or stale run identity does not cancel a run. | `routes/chat_routes.py`, `src/agent_runs.py` |
| Attach files | `POST /api/upload`, multipart repeated `files` fields and optional `session_id`. Returns `{files: [...]}` with IDs and metadata. Fetch an attachment through authenticated `GET /api/upload/{file_id}`. | `routes/upload_routes.py`, `static/js/fileHandler.js` |
| Model controls | `GET /api/prefs/model-generation`; `PUT` on the same URL with JSON `{model_key, options}`. Responses contain `{key, value}`; `value` maps model keys to controls. | `routes/prefs_routes.py`, `src/model_generation.py`, `static/js/modelGeneration.js` |
| Personas | `GET /api/presets`, `POST /api/presets/custom`, and template list/save/delete routes under `/api/presets/templates`. Existing mutation routes require admin privileges. | `routes/preset_routes.py`, `static/js/presets.js` |

Authentication currently follows the web client's session-cookie flow. Do not assume an integration `ody_` bearer token grants access to every client endpoint; integration tokens have explicit route and scope restrictions. Respect the server's account and ownership checks rather than copying profile files onto the phone.

## Streaming implementation details

A normal send includes `message`, `session`, `mode` (`chat` or `agent`), and `attachments` as a JSON string containing uploaded IDs. The web client also uses `plan_mode`, `approved_plan`, `allow_bash`, `allow_web_search`, `use_web`, `use_research`, `use_rag`, `preset_id`, `workspace`, and active document context when relevant. Preserve those semantics as their controls are ported.

Send `X-Tz-Name` as the device's IANA timezone and `X-Tz-Offset` as minutes **east of UTC**. The JavaScript uses the negative of `Date.getTimezoneOffset()`. This matters for calendar and task instructions.

SSE payloads include `data: {"delta":"..."}`, optional `thinking: true` on a text delta, typed events, `event: error` with a JSON error, and `data: [DONE]`. Decode UTF-8 incrementally and parse complete SSE frames across arbitrary network chunks. Preserve unknown event types without crashing. Render Markdown/code natively and keep thinking and tool activity separate from the final answer.

Important typed events include `model_info`, `attachments`, `tool_start`, `tool_output`, `tool_progress`, `agent_step`, `tool_approval_resolved`, `generated_image`, `metrics`, `message_saved`, `chat_terminal`, and research/document updates. Read their producers and the corresponding handlers in `static/js/chat.js` before defining Codable payloads. Agent approval continuations use the exact `tool_approval_id` and `tool_approval_decision`; implement an explicit native approval action rather than treating approval text as an ordinary new prompt.

The response header `X-Odysseus-Run-Id` identifies a detached run. Closing the stream, navigating away, or backgrounding the client does not stop ordinary Chat/Agent work on the server. The server saves the result. Detached runs are held in memory and do not survive a server-process restart; Compare has a separate lifecycle.

Resume replays the active run from the start; it is not a cursor-based continuation and does not currently expose SSE event IDs for `Last-Event-ID`. Rebuild the in-progress presentation for the run or otherwise deduplicate it, then reconcile with persisted history. Avoid duplicated bubbles, tool cards, and token counts. A lost send response must not trigger a blind second POST that repeats work. On foregrounding, check stream status, resume if active, otherwise reload history.

## Model controls and personas

The model preference key is the compact JSON encoding of `[endpoint_url.rstrip("/"), raw_model_id]`. Use the session's actual model endpoint URL, including its completion path when present, not the phone's Odysseus base URL. Preserve the raw model ID even when it is a long Linux model path. Do not serialize escaped slashes differently from the existing key; verify interoperability with keys returned by the server.

Controls already include thinking, reasoning effort and budget, temperature, output token limit, top-p, top-k, min-p, typical-p, seed, penalties, stop sequences, Mirostat, dynamic temperature, XTC, DRY, and sampler order. Read `GenerationOptions` for validation and `static/js/modelGeneration.js` for labels and default semantics. Blank fields mean use defaults; zero and false values must remain explicit. Saving an empty options object clears that model's override.

For the local Qwen runtime, Thinking Off is translated server-side into `chat_template_kwargs.enable_thinking = false`, `reasoning_effort = "none"`, and `reasoning_budget_tokens = 0`. The user-facing preference field is `reasoning_budget`. Preserve backend translation rather than implementing divergent provider rules on the phone. ChatGPT subscription endpoints use their existing capability-validated reasoning selector.

The stream request can optionally supply `generation_options` as JSON text, but when omitted the server loads the owner's saved model profile. The native UI should display a save success only after the server accepts the preference, and show load/save failures. Corey explicitly wants all available model controls accessible from chat.

Personality and identity are handled through the existing Persona workflow: a name and system prompt, optional generation defaults, and reusable templates. In the desktop interface this is More tools → Prompt → Persona → New → Save & Start Persona. Selecting the custom persona sends `preset_id: "custom"`. Follow the existing activation behavior and server privileges; do not imply every named template is globally active in every conversation.

## Attachments and desktop fixes to preserve

Upload images, videos, and documents to Odysseus before sending attachment IDs. Do not clear the pending attachment UI on failed upload, silently send only text, or convert all media into filename placeholders. Use authenticated downloads for thumbnails and playback; an unauthenticated image loader will fail against this server.

Image-only capability does not imply video capability. Recent desktop fixes inspect the native server's `/props`, pass supported videos as `input_video` bytes, and use the configured vision fallback when appropriate. The native client should send ordinary uploads and let the desktop select the actual model and media format. Corey's existing server has private FFmpeg and FFprobe installed; the binaries are not part of the source repository.

These accumulated desktop changes are included alongside this handoff:

- Hugging Face token synchronization and download behavior; cache scans across both Ubuntu and configured Windows directories; concurrent scans no longer overwrite one another's directory input.
- Model search improvements, including results containing “Uncensored,” and package maintenance in the private environment.
- Native GPU runtime preflight and serving diagnostics, plus WSL-aware GPU process discovery and VRAM clearing.
- Persistent per-owner, per-model generation controls applied in both Chat and Agent mode, including Thinking Off.
- Vision detection for local GGUF model paths and configured fallback models; native video handling and persistence of attachment references without embedding raw video bytes in chat history.
- Main-model selection excludes `mmproj` projector files, repairs stale selections, and rejects a projector used as the main model. The projector belongs in `--mmproj` beside the actual model.

Previously completed desktop verification included real image and video inference through the configured Qwen model with no reasoning output for Thinking Off, GPU availability, and browser checks for model/projector selection on desktop and mobile layouts. These checks establish desktop behavior only; they are not iOS validation.

## Validation on the Mac

Build and run the actual SwiftUI app in an iPhone simulator and an iPad size. Verify loading, empty, offline, permission-denied, authentication-expired, and error states. Keep native previews and mock API fixtures useful without requiring a running desktop, but also test the real desktop connection.

Required acceptance checks:

1. Sign in, handle 2FA if enabled, relaunch with remembered authentication, sign out, and switch servers without leaking the previous account or history.
2. Pick a server-provided model, create a chat, stream text, view persisted history, and switch between Chat and Agent. Render tool activity and explicit approval actions.
3. Background and reopen during generation; reconnect without duplicate text or tool events. Stop the exact run and verify it actually stops. Recover from a server restart by reloading saved history.
4. Save Thinking Off, temperature, and an explicit zero-valued setting; verify they persist on the desktop and phone and reach the next request. Display save errors rather than claiming success.
5. Upload and send an image, a supported video, and a document. Test failed and cancelled uploads, authenticated previews, and an image-only model's video fallback/error behavior.
6. Save and activate a persona, then verify both Chat and Agent receive the intended identity/system instructions. Respect non-admin restrictions.
7. Test on a physical device against the intended WSL server, including local-network permission handling, keyboard behavior, Dynamic Type, safe areas, and rotation.

Keep API decoding, SSE framing/replay, and model-key serialization covered by focused tests. For Python changes, use an isolated database (for example `DATABASE_URL=sqlite:///:memory:`) and the project environment; never run tests through the local production-profile launcher. Any new server endpoint should remain authenticated and owner-scoped.

The next developer should report a successful Xcode build, the simulator/device flows actually exercised, the installed artifact or project location, and any features or real-server checks still incomplete. Do not describe a source-only project as a tested or signed iOS build.

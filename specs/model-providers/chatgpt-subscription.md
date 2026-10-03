# ChatGPT Subscription Provider Shape

Last updated: 2026-10-03

## Scope

Canonical provider ID `chatgpt_subscription`; Codex Responses transport;
auth and runtime code in `src/chatgpt_subscription.py`,
`routes/chatgpt_subscription_routes.py`, and `src/llm_core.py`.
There is no dedicated ChatGPT Subscription canonical reader on current `dev`.

## Catalog Shape

The account-scoped Codex models endpoint returns root `models[]`; `slug` is the
request identity and `visibility`/`priority` control availability/order. These
fields do not prove tools, reasoning, vision, or context. Null/malformed model
lists fail soft rather than crashing discovery (#5280/#5281).

The canonical generic reader does not accept `slug`-only items, so this runtime
catalog is not currently normalized into `ModelCapabilityRecord` values.

## Request And Response Shape

Transport uses a ChatGPT backend Responses endpoint, `input` items, flattened
function tools, streamed function-call argument events, exact `call_id`, and
`function_call_output` continuation. Parallel calls and encrypted reasoning
continuity require preserving typed output/history rather than coercing all
roles to text. This shape is supported by the existing adapter and the focused
tool-calling follow-up evidence in #5490; unmerged observations remain claimed
until integrated/reproduced.

OAuth/device credentials and refresh are provider-session behavior. Expired
credentials should return an actionable reconnect error, not generic model
failure.

Subscription requests omit `temperature` and `max_output_tokens`, independently
of model naming. HTTP 403 permission and model-access failures retain their
actual reason; they do not imply expired credentials.

## Daybreak selection

The ChatGPT subscription model picker stores `daybreak_enabled` separately from
the model ID. New and migrated conversations default to false; a conversation's
saved choice survives reloads and forks. Preferences for new model selections
are scoped to the endpoint and model in the browser.

Requests explicitly select `access_programs.cyber`: `standard` when unchecked,
and the compatible Daybreak program when checked. Omission is not an off switch:
OpenAI may apply entitled access by default. Mainline models use `daybreak_blue`,
including models that require Red approval; Red model IDs use `daybreak_red`.
Daybreak aliases require their matching program, so a mainline model is needed
to use the same model with and without Daybreak. The server remains authoritative
for model compatibility and account entitlement. Access-program errors must not
be disguised as expired login or silently retried with another access program.

This control is scoped to the ChatGPT subscription Responses provider. Other
providers retain their existing transport and cannot enable this preference.
The choice is captured for each request so another conversation or a subsequent
checkbox change cannot alter a response already in progress.

Discovery reads each model's `available_access_programs.cyber` and persists the
result on its provider account. A known unavailable model has an unchecked,
disabled checkbox and an explanation that new requests use standard access.
Unknown availability remains selectable. Explicit model/program rejections are
remembered for that account; a successful catalog refresh replaces that data.
Neither discovering a restriction nor receiving a rejection retries a failed
request with Daybreak turned off.

Request contract: [OpenAI Daybreak guide](https://developers.openai.com/api/docs/guides/daybreak).

## Reasoning effort

The chatbox has a Reasoning effort selector beside the model name. Its choices
come from the connected account's `supported_reasoning_levels`; the catalog's
`default_reasoning_level` labels Provider default. The app does not assume every
model supports the same levels. If capability metadata is temporarily unavailable,
the same model's saved effort remains visible and applies to subsequent requests.
Users can reset it to Provider default, but cannot select unverified levels.
A new model with unknown capability starts at Provider default.

`reasoning_effort` is a nullable conversation preference. Existing conversations
start at Provider default; creation, history, reloads, and forks preserve an
explicit choice. An empty form value clears it. New-chat preferences are scoped
to the endpoint and model. Changing models keeps only compatible effort choices.

Each request captures its effort before asynchronous context work. The value
continues through chat, agent tool rounds, rewriting, and compaction using that
same model; a separately configured utility model keeps its own defaults.
Responses requests use `reasoning: {"effort": "high"}` for an explicit choice
and omit the field for Provider default. Daybreak and effort are independent.
This selector currently applies to ChatGPT Subscription; explicit effort must
not leak into a different provider or be silently downgraded during fallback.

Request contract: [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).

## Reconnecting

Authentication errors expose a Reconnect provider action that opens the
matching saved connection. Opening it never initiates sign-in or changes
credentials. The user starts reconnection from the form. When an exact
connection cannot be established, the UI asks the user to select a connection
from Added Models rather than choosing an account by URL alone.

ChatGPT device flows can bind an `endpoint_id` at start. Owner and provider are
validated before requesting a device code, and completion rechecks the captured
endpoint and authentication row before updating credentials. Reconnecting
preserves the connection's custom name and clears old account capabilities.

## Fallback And Safety

Only the explicit internal base/ChatGPT host selects this provider. Never send
subscription credentials to a custom OpenAI-compatible URL. Catalog slugs stay
identity-only unless account-scoped fields or probes supply capability.

## Current Gaps

- Comprehensive Responses tool/reasoning parity is still evolving.
- Account model slugs are not consumed by the canonical reader package.
- The account catalog does not currently provide a complete canonical
  capability card for every slug.

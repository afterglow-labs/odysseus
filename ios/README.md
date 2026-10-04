# Odysseus for iPhone and iPad

A native SwiftUI remote client for the existing Odysseus desktop server. iOS 17 or later. The desktop runs models, Python, tools, downloads, and persistent storage; the phone connects to the **Odysseus application URL**, not the model server URL.

Implemented: password and optional authenticator sign-in, Keychain sessions, searchable conversations and models, streaming Chat and Agent responses, stop/reconnect, model generation controls, personas, file/photo/video attachments, authenticated file previews, and agent approval cards. The interface follows Odysseus's dark palette, Fira Code typography, and monochrome icons.

This is the first native client, not full desktop feature parity. Brain, Calendar, Compare, Cookbook, Deep Research, Gallery, Library, Notes, Tasks, and desktop theme editing still need native screens. Generated media and internal document links do not yet open dedicated native workflows. Models and GPU launch settings remain managed on the desktop.

## Open and run

Open `Odysseus.xcodeproj` on a Mac with Xcode and an installed iOS simulator. Select the shared **Odysseus** scheme and an iPhone or iPad destination. There are no Swift package dependencies. The checked-in project is ready to open; after adding Swift files or resources, regenerate it with:

```bash
python3 scripts/generate_project.py
```

For a physical device, select your Apple development team in Signing & Capabilities. No team ID, certificate, provisioning profile, desktop password, or provider token is stored in this project.

The first screen accepts your desktop's base URL and Odysseus account. For WSL, use a reachable private HTTPS address, such as Tailscale Serve proxying the WSL app's `127.0.0.1:7860`. Enable Serve in the tailnet if needed, keep Tailscale connected on the phone, and enter the WSL machine's HTTPS name. A Windows localhost URL does not identify the WSL server from another device. The complete server address stays visible in chat and Connection settings.

The client validates TLS normally, rejects redirects to another origin, and keeps its session cookie in a server-scoped Keychain entry. Provider credentials remain on the desktop. HTTP local-network access depends on the device's ATS and local-network permissions; HTTPS is the preferred connection.

## Tests

List simulator IDs with `xcrun simctl list devices available`, then run:

```bash
scripts/test.sh <simulator-UDID>
```

The script starts and cleans up a loopback-only fixture on port 18766. It uses synthetic accounts, messages, and attachment bytes; it never opens the desktop profile. Do not run the fixture tests in parallel because they reset shared fixture state. Set `ODYSSEUS_DEVELOPMENT_TEAM` if your local signing setup requires it. Simulator tests must remain ad hoc signed so the Keychain is available; do not pass `CODE_SIGNING_ALLOWED=NO`.

Append `-only-testing:OdysseusTests` to run just the protocol and API tests.

Tests cover SSE fragmentation and UTF-8, exact model preference keys, explicit zero values, authentication/2FA, uploads and previews, saved controls, stream reconnection, stale-run stop rejection, personas and approval identity, and native UI flows. Fixture media verifies transport, not model vision inference. An iPhone connected to the actual desktop is still required to validate device permissions, photo/video import, and end-to-end model behavior.

In Debug builds only, `ODYSSEUS_TEST_SERVER` selects a fixture address and `ODYSSEUS_TEST_RESET_AUTH=1` clears that address's stored session. These overrides are excluded from Release builds. Tests dismiss the operating system's password-save prompt instead of disabling AutoFill in the app.

Verified October 4, 2026 with Xcode 27 beta: 14 protocol/API tests and all 3 native UI tests passed on an iPhone simulator running iOS 18.5. Login/chat/history/remembered sign-in and saved controls/stop also passed on iOS 27.0; the controls/chat/stop flow passed on an iPad running iOS 18.5. The device archive and development IPA export succeeded. These checks use the fixture; a physical-phone session and live model inference through the native app remain unverified.

## Development archive

```bash
export ODYSSEUS_DEVELOPMENT_TEAM=<your-Apple-team-ID>
scripts/archive.sh
```

Outputs are `build/Odysseus.xcarchive` and `build/export/Odysseus.ipa`. The export method is `debugging`: it installs on devices included in the development provisioning profile. This does not publish to TestFlight or the App Store. The existing desktop icon is reused for this development build; prepare and validate a distribution app-icon catalog before an App Store release.

When building over SSH, macOS may deny signing-key access from a background login even when the GUI session is unlocked. Run the build in the logged-in user's GUI session, or unlock the login Keychain locally on the Mac. Do not copy private signing keys to WSL or put a Keychain password in scripts.

## Structure

- `App/` owns connection, conversation, upload, and stream state.
- `Core/` contains the API, Keychain, SSE parser, and response models.
- `Views/` contains native navigation, chat, controls, personas, and account screens.
- `Resources/` reuses the existing Odysseus icon and Fira Code font; the font's SIL OFL license is included.
- `OdysseusTests/` and `OdysseusUITests/` exercise the API and native client through `scripts/fixture_server.py`.

See [IOS_HANDOFF.md](../IOS_HANDOFF.md) for inspected desktop API contracts, WSL details, and the remaining port roadmap.

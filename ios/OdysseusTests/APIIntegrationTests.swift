import XCTest
@testable import Odysseus

@MainActor
final class APIIntegrationTests: XCTestCase {
    private func waitUntil(_ condition: @MainActor () -> Bool) async throws {
        for _ in 0..<150 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTFail("Timed out waiting for the active server run")
    }

    func testDisconnectedRunResumesWithoutDuplicateTextAndStopsByRunID() async throws {
        let store = AppStore()
        store.serverAddress = "http://127.0.0.1:18766"
        store.username = "tester"
        await store.connect(password: "fixture-password", code: "", remember: false)
        XCTAssertTrue(store.authenticated)
        store.draft = "slow response for reconnect"
        await store.send()
        try await waitUntil { !store.stream.runID.isEmpty && !store.stream.entries.isEmpty }
        let runID = store.stream.runID
        let sid = try XCTUnwrap(store.conversation?.id)
        let api = try XCTUnwrap(store.api)
        let staleStop = try await api.stop(sessionID: sid, runID: "an-old-run")
        XCTAssertFalse(staleStop)
        store.suspend()
        try await waitUntil { !store.isStreaming }
        try await Task.sleep(for: .milliseconds(500))
        await store.resume()
        try await waitUntil { !store.stream.entries.isEmpty }
        XCTAssertEqual(store.stream.runID, runID)
        await store.stop()
        try await waitUntil { !store.isStreaming }
        XCTAssertFalse(store.needsReconnect)
        XCTAssertNil(store.error)
        let history = try await api.request("api/history/\(sid)?limit=50")
        let answers = store.messages.filter { $0.role == "assistant" }
        XCTAssertEqual(answers.count, 1)
        XCTAssertEqual(answers.first?.text, history["history"].array.last?["content"].string)
        XCTAssertEqual(store.messages.filter { $0.role == "user" }.count, 1)
        XCTAssertTrue(store.stream.entries.isEmpty)
        try await store.api?.logout()
    }

    func testAuthenticationUploadControlsStreamingAndLogout() async throws {
        let api = try OdysseusAPI(address: "http://127.0.0.1:18766")
        let status = try await api.login(username: "tester", password: "fixture-password", code: "", remember: false)
        XCTAssertTrue(status["authenticated"].bool)
        let models = ModelChoice.decode(try await api.request("api/models"))
        let model = try XCTUnwrap(models.first)
        let options = JSONValue.object(["thinking": .string("off"), "reasoning_effort": .string("none"), "reasoning_budget": .number(0), "temperature": .number(0)])
        _ = try await api.request("api/prefs/model-generation", method: "PUT", json: .object(["model_key": .string(model.preferenceKey), "options": options]))
        let preferences = try await api.request("api/prefs/model-generation")
        XCTAssertEqual(preferences["value"][model.preferenceKey], options)
        let conversation = try await api.request("api/session", method: "POST", form: ["model": model.model, "endpoint_id": model.endpointID, "endpoint_url": model.endpointURL])
        let sid = conversation["id"].string
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
        try Data("video transport fixture".utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        let uploaded = try await api.upload(PendingFile(url: file, name: "clip.mp4", mime: "video/mp4"), sessionID: sid)
        XCTAssertEqual(uploaded.mime, "video/mp4")
        let downloaded = try await api.download(uploaded)
        defer { try? FileManager.default.removeItem(at: downloaded) }
        XCTAssertEqual(try Data(contentsOf: downloaded), Data("video transport fixture".utf8))
        var runID = "", answer = ""
        try await api.stream(path: "api/chat_stream", fields: ["session": sid, "message": "Describe this video", "mode": "chat", "attachments": JSONValue.array([.string(uploaded.id)]).text], started: { runID = $0 }, receive: { answer += $0["delta"].string })
        XCTAssertFalse(runID.isEmpty)
        XCTAssertEqual(answer, "Your native client reached the desktop API.")
        let history = try await api.request("api/history/\(sid)?limit=50")
        XCTAssertEqual(history["history"].array.first?["metadata"]["attachments"].array.first?["id"].string, uploaded.id)
        try await api.logout()
        let signedOut = try await api.request("api/auth/status")
        XCTAssertFalse(signedOut["authenticated"].bool)
    }

    func testCancelledUploadKeepsFileAndDraftAndSendingPreservesNewTyping() async throws {
        let store = AppStore()
        store.serverAddress = "http://127.0.0.1:18766"; store.username = "tester"
        await store.connect(password: "fixture-password", code: "", remember: false)
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("slow-upload.txt")
        try Data("attachment content".utf8).write(to: file)
        defer { try? FileManager.default.removeItem(at: folder) }
        try store.addFile(file)
        store.draft = "Send this file"
        store.submit()
        try await waitUntil { !store.uploadStatus.isEmpty }
        store.cancelUpload()
        try await waitUntil { !store.busy }
        XCTAssertNil(store.error)
        XCTAssertEqual(store.draft, "Send this file")
        XCTAssertEqual(store.pendingFiles.count, 1)
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.pendingFiles[0].url.path))
        XCTAssertFalse(store.isStreaming)
        store.submit()
        try await waitUntil { !store.uploadStatus.isEmpty }
        store.draft = "My next message"
        try await waitUntil { store.isStreaming }
        try await waitUntil { !store.isStreaming }
        XCTAssertNil(store.error)
        XCTAssertEqual(store.draft, "My next message")
        XCTAssertTrue(store.pendingFiles.isEmpty)
        XCTAssertEqual(store.messages.filter { $0.role == "user" }.first?.text, "Send this file")
        try await store.api?.logout()
    }

    func testTwoFactorChallengeDoesNotCountAsAuthentication() async throws {
        let api = try OdysseusAPI(address: "http://127.0.0.1:18766")
        do {
            _ = try await api.login(username: "twofactor", password: "fixture-password", code: "", remember: false)
            XCTFail("Expected a 2FA challenge")
        } catch ClientError.requiresTOTP { }
        do {
            _ = try await api.login(username: "twofactor", password: "fixture-password", code: "000000", remember: false)
            XCTFail("An invalid authenticator code must not sign in")
        } catch ClientError.http(401, _) { }
        let result = try await api.login(username: "twofactor", password: "fixture-password", code: "123456", remember: false)
        XCTAssertTrue(result["authenticated"].bool)
        try await api.logout()
    }

    func testPersonaPersistsForConversationAndAgentApprovalUsesExactIdentity() async throws {
        let store = AppStore()
        store.serverAddress = "http://127.0.0.1:18766"; store.username = "tester"
        await store.connect(password: "fixture-password", code: "", remember: false)
        let api = try XCTUnwrap(store.api)
        _ = try await api.request("api/presets/custom", method: "POST", json: .object(["name": .string("Test persona"), "system_prompt": .string("Be concise."), "enabled": .bool(true)]))
        store.selectPersona("custom"); store.mode = "agent"; store.draft = "Request approval"
        await store.send()
        try await waitUntil { !store.isStreaming }
        XCTAssertFalse(store.stream.approval["approval_id"].string.isEmpty)
        let approval = store.stream.approval
        let conversation = try XCTUnwrap(store.conversation)
        store.newChat()
        XCTAssertEqual(store.personaID, "")
        await store.open(conversation)
        XCTAssertEqual(store.personaID, "custom")
        XCTAssertEqual(store.stream.approval["approval_id"], approval["approval_id"])
        XCTAssertTrue(store.messages.contains { $0.role == "tool" && $0.text.contains("Fixture tool output") })
        await store.send(approval: approval, decision: "approve_task")
        try await waitUntil { !store.isStreaming }
        XCTAssertNil(store.error)
        XCTAssertTrue(store.stream.approval.isNull)
        let requests = try await api.request("test/state")["requests"].array
        let continuation = try XCTUnwrap(requests.last)
        XCTAssertEqual(continuation["tool_approval_id"], approval["approval_id"])
        XCTAssertEqual(continuation["tool_approval_decision"].string, "approve_task")
        XCTAssertEqual(continuation["mode"].string, "agent")
        XCTAssertEqual(continuation["preset_id"].string, "custom")
        try await api.logout()
    }
}

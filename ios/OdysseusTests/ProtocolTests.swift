import XCTest
@testable import Odysseus

final class ProtocolTests: XCTestCase {
    func testSSEParsesFragmentedUTF8AndCRLF() throws {
        let source = ": heartbeat\r\nevent: message\r\ndata: {\"delta\":\"Hello, 世界\"}\r\n\r\ndata: [DONE]\n\n"
        var parser = SSEParser()
        var frames: [SSEFrame] = []
        for byte in Data(source.utf8) { frames += try parser.feed(Data([byte])) }
        XCTAssertEqual(frames, [SSEFrame(event: "message", data: "{\"delta\":\"Hello, 世界\"}"), SSEFrame(event: "message", data: "[DONE]")])
    }

    func testSSEMultilineAndFinalFrame() throws {
        var parser = SSEParser()
        XCTAssertEqual(try parser.feed(Data("event: error\ndata: first\ndata: second".utf8)), [])
        XCTAssertEqual(try parser.finish(), [SSEFrame(event: "error", data: "first\nsecond")])
    }

    func testModelPreferenceKeyMatchesPythonAndJavaScript() {
        XCTAssertEqual(ModelChoice.preferenceKey(endpointURL: "http://localhost:8001/v1/chat/completions/", model: "/home/corey/models/Qwen-世界.gguf"),
                       "[\"http://localhost:8001/v1/chat/completions\",\"/home/corey/models/Qwen-世界.gguf\"]")
    }

    func testExplicitZeroAndFalseSurviveJSONEncoding() throws {
        let value = JSONValue.object(["temperature": .number(0), "reasoning_budget": .number(0), "enabled": .bool(false)])
        XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: value.encoded()), value)
    }

    @MainActor func testServerValidationRejectsCredentialsAndMissingScheme() throws {
        for invalid in ["localhost:7860", "https://user:password@example.com", "file:///private/data", "https://example.com?token=secret", "https://example.com/#fragment"] {
            XCTAssertThrowsError(try OdysseusAPI.normalizeServer(invalid))
        }
        XCTAssertEqual(try OdysseusAPI.normalizeServer(" HTTPS://Desktop.Example:7860/ ").absoluteString, "https://desktop.example:7860")
    }

    @MainActor func testMultipartFieldsPreserveAttachmentIDsAndReasoningOff() throws {
        let data = OdysseusAPI.formData(["message": "a+b & 世界", "attachments": "[\"upload-id\"]", "generation_options": "{\"thinking\":\"off\",\"reasoning_budget\":0}"], boundary: "test-boundary")
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(text.contains("a+b & 世界"))
        XCTAssertTrue(text.contains("name=\"attachments\"\r\n\r\n[\"upload-id\"]"))
        XCTAssertTrue(text.hasSuffix("--test-boundary--\r\n"))
    }

    func testToolApprovalAndThinkingAreSeparateFromAnswer() {
        var live = StreamPresentation()
        live.receive(.object(["delta": .string("Reasoning"), "thinking": .bool(true)]))
        live.receive(.object(["delta": .string("Answer")]))
        let approval = JSONValue.object(["approval_id": .string("opaque-approval"), "kind": .string("tool_approval")])
        live.receive(.object(["type": .string("tool_output"), "ask_user": approval]))
        XCTAssertEqual(live.entries[0].text, "Answer")
        XCTAssertEqual(live.entries[0].thinking, "Reasoning")
        XCTAssertEqual(live.approval, approval)
    }

    func testPaginationHasStableDistinctFallbackIDs() {
        let entry = JSONValue.object(["role": .string("assistant"), "content": .string("Hello")])
        let first = ChatEntry.history(.object(["history": .array([entry]), "offset": .number(0)]))
        let second = ChatEntry.history(.object(["history": .array([entry]), "offset": .number(50)]))
        XCTAssertNotEqual(first[0].id, second[0].id)
    }

    func testHistoryKeepsToolsAndThinkingWithoutReopeningResolvedApproval() {
        let history = JSONValue.object(["history": .array([.object([
            "role": .string("assistant"), "content": .string("Completed"),
            "metadata": .object(["thinking": .string("Saved reasoning"), "tool_events": .array([.object([
                "tool": .string("write_file"), "output": .string("Written"),
                "ask_user": .object(["approval_id": .string("used-approval"), "resolved": .string("approve_task")])
            ])])])
        ])])])
        let entries = ChatEntry.history(history)
        XCTAssertTrue(entries.contains { $0.role == "tool" && $0.text.contains("Written") })
        XCTAssertEqual(entries.last?.thinking, "Saved reasoning")
        XCTAssertEqual(entries.last?.text, "Completed")
        XCTAssertEqual(entries.last?.approval, .null)
    }
}

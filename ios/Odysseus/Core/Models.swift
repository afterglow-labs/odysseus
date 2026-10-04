import Foundation

struct Conversation: Identifiable, Hashable {
    let id: String
    var name: String
    var model: String
    var endpointURL: String
    var mode: String
    var reasoningEffort: String
    init(_ json: JSONValue) {
        id = json["id"].string
        name = json["name"].string.isEmpty ? "New chat" : json["name"].string
        model = json["model"].string
        endpointURL = json["endpoint_url"].string
        mode = json["mode"].string == "agent" ? "agent" : "chat"
        reasoningEffort = json["reasoning_effort"].string
    }
}

struct ModelChoice: Identifiable, Hashable {
    let endpointID: String
    let endpointName: String
    let endpointURL: String
    let model: String
    let label: String
    var reasoningLevels: [String] = []
    var defaultReasoning: String = ""
    var id: String { endpointID + "|" + model }
    var preferenceKey: String { Self.preferenceKey(endpointURL: endpointURL, model: model) }

    static func preferenceKey(endpointURL: String, model: String) -> String {
        var url = endpointURL
        while url.hasSuffix("/") { url.removeLast() }
        return JSONValue.array([.string(url), .string(model)]).text
    }

    static func decode(_ response: JSONValue) -> [ModelChoice] {
        response["items"].array.flatMap { item in
            let ids = item["models"].array + item["models_extra"].array
            let labels = item["models_display"].array + item["models_extra_display"].array
            return ids.enumerated().map { index, model in
                let label = index < labels.count ? labels[index].string : ""
                return ModelChoice(endpointID: item["endpoint_id"].string,
                                   endpointName: item["endpoint_name"].string,
                                   endpointURL: item["url"].string, model: model.string,
                                   label: label.isEmpty ? model.string : label,
                                   reasoningLevels: item["reasoning_models"][model.string]["levels"].array.map(\.string),
                                   defaultReasoning: item["reasoning_models"][model.string]["default"].string)
            }
        }
    }
}

struct Attachment: Identifiable, Equatable {
    let id: String
    let name: String
    let mime: String
    init(_ json: JSONValue) { id = json["id"].string; name = json["name"].string; mime = json["mime"].string }
}

struct PendingFile: Identifiable {
    let id = UUID()
    let url: URL
    let name: String
    let mime: String
    var uploaded: Attachment?
}

struct ChatEntry: Identifiable {
    var id: String
    var role: String
    var text: String
    var thinking: String = ""
    var attachments: [Attachment] = []
    var approval: JSONValue = .null

    static func history(_ response: JSONValue) -> [ChatEntry] {
        response["history"].array.enumerated().flatMap { index, item -> [ChatEntry] in
            let content = item["content"]
            let text = content.array.isEmpty ? content.string : content.array.compactMap { part -> String? in
                if part["type"].string == "text" { return part["text"].string }
                return nil
            }.joined(separator: "\n")
            let metadata = item["metadata"]
            let rawID = metadata["_db_id"].text
            let offset = Int(response["offset"].number ?? 0)
            let id = rawID.isEmpty ? "history-\(offset + index)" : "db-\(rawID)"
            let events = metadata["tool_events"].array
            let approval = unresolvedApproval(events)
            let tools = events.enumerated().map { number, event in
                ChatEntry(id: "\(id)-tool-\(number)", role: "tool", text: [event["tool"].string, event["command"].string, event["output"].text].filter { !$0.isEmpty }.joined(separator: "\n"))
            }
            return tools + [ChatEntry(id: id, role: item["role"].string, text: text,
                                     thinking: metadata["thinking"].string,
                                     attachments: metadata["attachments"].array.map(Attachment.init), approval: approval)]
        }
    }

    private static func unresolvedApproval(_ events: [JSONValue]) -> JSONValue {
        for event in events.reversed() {
            let question = event["ask_user"]
            guard !question.isNull else { continue }
            switch question["resolved"] {
            case .null, .bool(false), .string(""): return question
            default: continue
            }
        }
        return .null
    }
}

struct StreamPresentation {
    private(set) var entries: [ChatEntry] = []
    private var sequence = 0
    var status = "Waiting for the model…"
    var runID = ""
    var approval: JSONValue = .null

    mutating func receive(_ json: JSONValue) {
        let delta = json["delta"].string
        if !delta.isEmpty {
            if entries.last?.role != "assistant" {
                sequence += 1
                entries.append(ChatEntry(id: "live-\(sequence)", role: "assistant", text: ""))
            }
            if json["thinking"].bool { entries[entries.count - 1].thinking += delta }
            else { entries[entries.count - 1].text += delta }
            status = json["thinking"].bool ? "Thinking…" : "Responding…"
        }
        let type = json["type"].string
        if type == "tool_start" {
            sequence += 1
            entries.append(ChatEntry(id: "live-\(sequence)", role: "tool", text: json["tool"].string + "\n" + json["command"].string))
            status = "Running \(json["tool"].string)…"
        } else if type == "tool_output" {
            let output = json["output"].isNull ? json["data"] : json["output"]
            if entries.last?.role == "tool" { entries[entries.count - 1].text += "\n" + output.text }
            let candidate = !json["ask_user"].isNull ? json["ask_user"] : output["ask_user"]
            if !candidate.isNull { approval = candidate }
        } else if type == "ask_user" { approval = json["data"] }
        else if type == "tool_progress" || type == "research_progress" {
            let detail = json["message"].string
            if !detail.isEmpty { status = detail }
        } else if type == "agent_prep" { status = "Preparing agent…" }
        else if type == "tool_approval_resolved" { approval = .null }
    }
}

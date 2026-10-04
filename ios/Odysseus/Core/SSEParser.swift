import Foundation

struct SSEFrame: Equatable {
    let event: String
    let data: String
}

struct SSEParser {
    private var buffer = Data()
    private var event = "message"
    private var lines: [String] = []

    mutating func feed(_ chunk: Data) throws -> [SSEFrame] {
        buffer.append(chunk)
        guard buffer.count < 32 * 1024 * 1024 else { throw ClientError.message("The server sent an oversized stream event.") }
        var frames: [SSEFrame] = []
        while let index = buffer.firstIndex(of: 10) {
            var bytes = buffer.prefix(upTo: index)
            buffer.removeSubrange(...index)
            if bytes.last == 13 { bytes = bytes.dropLast() }
            guard let line = String(data: bytes, encoding: .utf8) else { throw ClientError.message("The server sent invalid UTF-8.") }
            if let frame = consume(line) { frames.append(frame) }
        }
        return frames
    }

    mutating func consume(_ line: String) -> SSEFrame? {
        if line.isEmpty {
            defer { event = "message"; lines = [] }
            return lines.isEmpty ? nil : SSEFrame(event: event, data: lines.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        let split = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        var value = split.count > 1 ? String(split[1]) : ""
        if value.hasPrefix(" ") { value.removeFirst() }
        if split[0] == "event" { event = value }
        else if split[0] == "data" { lines.append(value) }
        return nil
    }

    mutating func finish() throws -> [SSEFrame] {
        var result: [SSEFrame] = []
        if !buffer.isEmpty { result = try feed(Data([10])) }
        if let frame = consume("") { result.append(frame) }
        return result
    }
}

enum ClientError: LocalizedError {
    case message(String)
    case http(Int, String)
    case requiresTOTP
    var errorDescription: String? {
        switch self {
        case .message(let text): return text
        case .http(let code, let text): return "\(text) (HTTP \(code))"
        case .requiresTOTP: return "Enter the six-digit code from your authenticator."
        }
    }
}

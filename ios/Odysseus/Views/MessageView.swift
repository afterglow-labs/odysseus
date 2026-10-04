import SwiftUI

struct MessageView: View {
    let message: ChatEntry
    let preview: (Attachment) -> Void

    private var parts: (answer: String, thinking: String) {
        var remaining = message.text
        var answer = "", thinking = message.thinking
        while let start = remaining.range(of: "<think>", options: .caseInsensitive) {
            answer += String(remaining[..<start.lowerBound])
            remaining = String(remaining[start.upperBound...])
            if let end = remaining.range(of: "</think>", options: .caseInsensitive) {
                thinking += String(remaining[..<end.lowerBound])
                remaining = String(remaining[end.upperBound...])
            } else { thinking += remaining; remaining = "" }
        }
        answer += remaining
        return (answer.trimmingCharacters(in: .whitespacesAndNewlines), thinking)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Image(systemName: message.role == "user" ? "person.crop.circle" : message.role == "tool" ? "terminal" : "sparkle")
                Text(message.role == "user" ? "You" : message.role == "tool" ? "Tool activity" : "Odysseus")
            }.font(.caption.weight(.semibold)).foregroundStyle(message.role == "user" ? .secondary : OdysseusTheme.foreground)
            if message.role == "tool" {
                DisclosureGroup("Details") { Text(message.text).font(.caption.monospaced()).textSelection(.enabled) }
            } else {
                if !parts.thinking.isEmpty {
                    DisclosureGroup("Thinking") { Text(parts.thinking).font(.callout).foregroundStyle(.secondary).textSelection(.enabled) }
                }
                if !parts.answer.isEmpty { MarkdownText(text: parts.answer) }
            }
            ForEach(message.attachments) { attachment in
                Button { preview(attachment) } label: {
                    Label(attachment.name, systemImage: attachment.mime.hasPrefix("video/") ? "film" : attachment.mime.hasPrefix("image/") ? "photo" : "doc")
                }.font(.caption).buttonStyle(.bordered)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(message.role == "user" ? 14 : 0)
        .background(message.role == "user" ? OdysseusTheme.panel : .clear, in: RoundedRectangle(cornerRadius: 14))
        .contextMenu { Button("Copy", systemImage: "doc.on.doc") { UIPasteboard.general.string = parts.answer }; ShareLink(item: parts.answer) }
    }
}

struct MarkdownText: View {
    let text: String
    private var segments: [String] { text.components(separatedBy: "```") }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(segments.enumerated()), id: \.offset) { index, segment in
                if index.isMultiple(of: 2) {
                    Text((try? AttributedString(markdown: segment, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(segment)).textSelection(.enabled)
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack { Text("Code").font(.caption); Spacer(); Button("Copy") { UIPasteboard.general.string = code(segment) }.font(.caption) }
                        ScrollView(.horizontal) { Text(code(segment)).font(.callout.monospaced()).textSelection(.enabled) }
                    }.padding(12).background(OdysseusTheme.panel, in: RoundedRectangle(cornerRadius: 10))
                }
            }
        }
    }
    private func code(_ value: String) -> String {
        guard let newline = value.firstIndex(of: "\n") else { return value }
        return String(value[value.index(after: newline)...]).trimmingCharacters(in: .newlines)
    }
}

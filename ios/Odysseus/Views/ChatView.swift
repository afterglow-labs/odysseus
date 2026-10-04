import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import QuickLook

private struct ImportedMedia: Transferable {
    let url: URL
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .image) { received in try copy(received.file) }
        FileRepresentation(importedContentType: .movie) { received in try copy(received.file) }
    }
    private static func copy(_ url: URL) throws -> ImportedMedia {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let target = directory.appendingPathComponent(url.lastPathComponent)
        try FileManager.default.copyItem(at: url, to: target)
        return ImportedMedia(url: target)
    }
}

struct ChatView: View {
    @Environment(AppStore.self) private var store
    @Binding var sheet: AppSheet?
    @State private var importingFiles = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var previewURL: URL?
    @State private var importingMedia = false
    @FocusState private var composerFocused: Bool

    var body: some View {
        @Bindable var store = store
        VStack(spacing: 0) {
            HStack(spacing: 7) {
                Circle().fill(store.needsReconnect ? Color.orange : Color.green).frame(width: 6, height: 6)
                Text(store.serverLabel).lineLimit(1).truncationMode(.middle)
                Spacer()
                if store.busy { ProgressView().controlSize(.small) }
            }.font(.caption2).foregroundStyle(.secondary).padding(.horizontal).padding(.vertical, 8)
            Divider().overlay(OdysseusTheme.border)
            transcript
            if !store.stream.approval.isNull { ApprovalCard(approval: store.stream.approval) }
            if let error = store.error {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.circle")
                    Text(error).font(.caption).textSelection(.enabled)
                    Spacer(minLength: 0)
                    Button { store.error = nil } label: { Image(systemName: "xmark") }.accessibilityLabel("Dismiss error")
                }.foregroundStyle(OdysseusTheme.coral).padding(12).background(OdysseusTheme.panel)
            }
            if store.needsReconnect, let conversation = store.conversation {
                Button { Task { await store.open(conversation) } } label: { Label("Reconnect and check response", systemImage: "arrow.clockwise") }
                    .padding(10).accessibilityIdentifier("reconnectButton")
            }
        }
        .background(OdysseusTheme.background)
        .safeAreaInset(edge: .bottom, spacing: 0) { composer }
        .fileImporter(isPresented: $importingFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            do { for url in try result.get() { try store.addFile(url) } } catch { store.report(error) }
        }
        .onChange(of: photos) { _, items in
            Task {
                importingMedia = true
                defer { importingMedia = false; photos = [] }
                for item in items {
                    do {
                        if let media = try await item.loadTransferable(type: ImportedMedia.self) {
                            defer { try? FileManager.default.removeItem(at: media.url) }
                            try store.addFile(media.url)
                        } else { store.error = "That photo or video could not be imported. Try choosing it from Files." }
                    } catch { store.report(error) }
                }
            }
        }
        .quickLookPreview($previewURL)
    }

    private var transcript: some View {
        ScrollViewReader { reader in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    if store.hasOlderHistory {
                        Button("Load earlier messages") { Task { await store.loadOlder() } }.frame(maxWidth: .infinity)
                    }
                    if store.messages.isEmpty && !store.isStreaming {
                        VStack(alignment: .leading, spacing: 12) {
                            Image(systemName: "sparkle").font(.largeTitle).foregroundStyle(OdysseusTheme.coral)
                            Text("New chat ready.").font(.title2.weight(.semibold))
                            Text("Ask a question, attach a file, or switch to Agent to work with your desktop tools.")
                                .foregroundStyle(.secondary)
                            if store.models.isEmpty { Text("No models are available. Start a model or configure an endpoint on your desktop, then refresh.").font(.callout).foregroundStyle(OdysseusTheme.coral) }
                        }.padding(.vertical, 32).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    ForEach(store.messages + store.stream.entries) { message in
                        MessageView(message: message) { attachment in
                            Task { do { previewURL = try await store.api?.download(attachment) } catch { store.report(error) } }
                        }
                    }
                    if store.isStreaming {
                        HStack { ProgressView().controlSize(.small); Text(store.stream.status).font(.caption).foregroundStyle(.secondary) }
                            .accessibilityIdentifier("streamStatus")
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }.padding(18)
            }
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(.bottom)
            .onChange(of: store.messages.count) { _, _ in reader.scrollTo("bottom", anchor: .bottom) }
            .onChange(of: store.stream.entries.last?.text.count ?? 0) { _, _ in
                if store.isStreaming { reader.scrollTo("bottom", anchor: .bottom) }
            }
        }
    }

    private var composer: some View {
        @Bindable var store = store
        return VStack(spacing: 10) {
            HStack(spacing: 12) {
                Button { sheet = .models } label: {
                    HStack(spacing: 5) { Image(systemName: "cpu"); Text(store.selectedModel?.label ?? "Choose model").lineLimit(1); Image(systemName: "chevron.down").font(.caption2) }
                }.font(.caption).accessibilityIdentifier("modelPickerButton").disabled(store.isStreaming || store.busy)
                Spacer(minLength: 0)
                Button { if let model = store.selectedModel { sheet = .controls(model) } } label: { Image(systemName: "slider.horizontal.3").frame(width: 44, height: 44).contentShape(Rectangle()) }
                    .accessibilityLabel("Model controls").accessibilityIdentifier("modelControlsButton").disabled(store.selectedModel == nil)
                Button { sheet = .persona } label: { Image(systemName: store.personaID.isEmpty ? "person.text.rectangle" : "person.text.rectangle.fill").frame(width: 44, height: 44).contentShape(Rectangle()) }
                    .accessibilityLabel("Persona")
            }
            if !store.pendingFiles.isEmpty {
                ScrollView(.horizontal) {
                    HStack {
                        ForEach(store.pendingFiles) { file in
                            HStack {
                                Image(systemName: file.mime.hasPrefix("video/") ? "film" : "paperclip")
                                Text(file.name).lineLimit(1)
                                Button { store.removeFile(file.id) } label: { Image(systemName: "xmark.circle.fill") }.accessibilityLabel("Remove \(file.name)").disabled(store.busy)
                            }.font(.caption).padding(8).background(OdysseusTheme.border.opacity(0.25), in: Capsule())
                        }
                    }
                }
            }
            if importingMedia || !store.uploadStatus.isEmpty {
                HStack {
                    ProgressView().controlSize(.small)
                    Text(importingMedia ? "Importing attachment…" : store.uploadStatus).font(.caption)
                    Spacer(minLength: 0)
                    if !store.uploadStatus.isEmpty { Button("Cancel") { store.cancelUpload() }.font(.caption).accessibilityIdentifier("cancelUpload") }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(alignment: .bottom, spacing: 10) {
                Menu {
                    Button("Choose files", systemImage: "folder") { importingFiles = true }
                    PhotosPicker(selection: $photos, maxSelectionCount: 8, matching: .any(of: [.images, .videos])) { Label("Photos and videos", systemImage: "photo.on.rectangle") }
                } label: { Image(systemName: "plus").frame(width: 28, height: 35) }.accessibilityLabel("Add attachments").disabled(store.busy || importingMedia)
                TextField("Message Odysseus", text: $store.draft, axis: .vertical)
                    .lineLimit(1...6).focused($composerFocused).padding(.vertical, 8).accessibilityIdentifier("messageInput")
                if store.isStreaming {
                    Button { Task { await store.stop() } } label: { Image(systemName: "stop.fill").frame(width: 35, height: 35).background(OdysseusTheme.coral, in: Circle()).foregroundStyle(.black) }
                        .accessibilityLabel("Stop generation").accessibilityIdentifier("stopButton")
                } else {
                    Button { composerFocused = false; store.submit() } label: { Image(systemName: "arrow.up").frame(width: 35, height: 35).background(OdysseusTheme.coral, in: Circle()).foregroundStyle(.black) }
                        .accessibilityLabel("Send message").accessibilityIdentifier("sendButton")
                        .disabled(store.busy || importingMedia || store.needsReconnect || store.selectedModel == nil || (store.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && store.pendingFiles.isEmpty))
                }
            }.padding(8).background(OdysseusTheme.panel, in: RoundedRectangle(cornerRadius: 16)).overlay(RoundedRectangle(cornerRadius: 16).stroke(OdysseusTheme.border))
            HStack {
                Picker("Mode", selection: $store.mode) { Text("Chat").tag("chat"); Text("Agent").tag("agent") }
                    .pickerStyle(.segmented).frame(maxWidth: 210).disabled(store.isStreaming).accessibilityIdentifier("modePicker")
                Spacer()
                Menu {
                    Toggle("Plan mode", isOn: $store.planMode)
                    Toggle("Allow shell tools", isOn: $store.allowShell)
                    Toggle("Web search", isOn: $store.allowWeb)
                } label: { Image(systemName: "ellipsis.circle").padding(6) }.accessibilityLabel("Chat options")
            }
        }.padding(12).background(OdysseusTheme.background)
    }
}

struct ApprovalCard: View {
    @Environment(AppStore.self) private var store
    let approval: JSONValue
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(approval["question"].string).font(.headline)
            Text(approval["description"].string).font(.caption)
            if !approval["action"].isNull {
                DisclosureGroup("Review action") { Text(approval["action"]["content"].text).font(.caption.monospaced()).textSelection(.enabled) }
            }
            ForEach(Array(approval["options"].array.enumerated()), id: \.offset) { _, option in
                Button(option["label"].string.isEmpty ? option.text : option["label"].string) {
                    if approval["approval_id"].string.isEmpty {
                        store.draft = option["label"].string.isEmpty ? option.text : option["label"].string
                        store.submit()
                    } else { store.submit(approval: approval, decision: option["value"].string) }
                }
                    .disabled(store.isStreaming || store.busy)
                Text(option["description"].string).font(.caption2).foregroundStyle(.secondary)
            }
        }.padding(14).background(OdysseusTheme.panel).clipShape(RoundedRectangle(cornerRadius: 12)).padding(.horizontal)
    }
}

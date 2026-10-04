import Foundation
import Observation
import UniformTypeIdentifiers

@MainActor @Observable
final class AppStore {
    var serverAddress = UserDefaults.standard.string(forKey: "odysseus.server") ?? ""
    var username = ""
    var authenticated = false
    var isAdmin = false
    var requiresTOTP = false
    var error: String?
    var busy = false
    var conversations: [Conversation] = []
    var models: [ModelChoice] = []
    var selectedModelID = ""
    var conversation: Conversation?
    var messages: [ChatEntry] = []
    var stream = StreamPresentation()
    var isStreaming = false
    var needsReconnect = false
    var draft = ""
    var pendingFiles: [PendingFile] = []
    var uploadStatus = ""
    var mode = "chat"
    var planMode = false
    var allowShell = false
    var allowWeb = false
    var personaID = ""
    var reasoningEffort = ""
    var historyOffset = 0
    var hasOlderHistory = false
    private(set) var api: OdysseusAPI?
    private var streamTask: Task<Void, Never>?
    private var sendTask: Task<Void, Never>?
    private var selectionEpoch = UUID()
    private var draftCache: [String: String] = [:]
    private var fileCache: [String: [PendingFile]] = [:]

    private func personaKey(_ conversationID: String? = nil) -> String {
        "odysseus.persona." + JSONValue.array([.string(serverLabel), .string(username), .string(conversationID ?? conversation?.id ?? "new")]).text
    }

    func selectPersona(_ id: String) {
        personaID = id
        UserDefaults.standard.set(id, forKey: personaKey())
    }

    private func restorePersona() { personaID = UserDefaults.standard.string(forKey: personaKey()) ?? "" }

    init() {
        #if DEBUG
        if let address = ProcessInfo.processInfo.environment["ODYSSEUS_TEST_SERVER"] {
            serverAddress = address
            if ProcessInfo.processInfo.environment["ODYSSEUS_TEST_RESET_AUTH"] == "1",
               let url = try? OdysseusAPI.normalizeServer(address) {
                try? CredentialVault.save(nil, server: url.absoluteString)
            }
        }
        #endif
    }

    var selectedModel: ModelChoice? { models.first { $0.id == selectedModelID } }
    var serverLabel: String { api?.baseURL.absoluteString ?? serverAddress }

    func report(_ error: Error) {
        guard !(error is CancellationError), (error as NSError).code != NSURLErrorCancelled else { return }
        self.error = error.localizedDescription
        if case ClientError.http(401, _) = error { authenticated = false }
    }

    func restore() async {
        guard !serverAddress.isEmpty, !authenticated else { return }
        do {
            let client = try OdysseusAPI(address: serverAddress)
            api = client
            let status = try await client.request("api/auth/status")
            if status["authenticated"].bool { try await finishConnection(status) }
        } catch { report(error) }
    }

    func connect(password: String, code: String, remember: Bool) async {
        busy = true; error = nil
        defer { busy = false }
        do {
            api?.invalidate()
            let client = try OdysseusAPI(address: serverAddress)
            api = client
            let status = try await client.request("api/auth/status")
            if status["authenticated"].bool { try await finishConnection(status) }
            else { try await finishConnection(try await client.login(username: username, password: password, code: code, remember: remember)) }
            requiresTOTP = false
        } catch ClientError.requiresTOTP { requiresTOTP = true; self.error = ClientError.requiresTOTP.localizedDescription }
        catch { report(error) }
    }

    private func finishConnection(_ status: JSONValue) async throws {
        guard status["authenticated"].bool else { throw ClientError.message("The server did not establish an authenticated session.") }
        streamTask?.cancel(); selectionEpoch = UUID()
        conversation = nil; messages = []; stream = StreamPresentation()
        isStreaming = false; needsReconnect = false
        for file in pendingFiles + fileCache.values.flatMap({ $0 }) { try? FileManager.default.removeItem(at: file.url) }
        pendingFiles = []; fileCache = [:]; draftCache = [:]; draft = ""
        username = status["username"].string
        isAdmin = status["is_admin"].bool
        authenticated = true
        serverAddress = api?.baseURL.absoluteString ?? serverAddress
        UserDefaults.standard.set(serverAddress, forKey: "odysseus.server")
        restorePersona()
        try await refresh()
    }

    func refresh() async throws {
        guard let api else { return }
        let list = try await api.request("api/sessions")
        conversations = list.array.map(Conversation.init)
        models = ModelChoice.decode(try await api.request("api/models"))
        if let conversation {
            selectedModelID = models.first { $0.model == conversation.model && $0.endpointURL == conversation.endpointURL }?.id ?? ""
        } else if !models.contains(where: { $0.id == selectedModelID }) { selectedModelID = models.first?.id ?? "" }
    }

    func logout() async {
        busy = true
        defer { busy = false }
        do {
            try await api?.logout()
            streamTask?.cancel(); selectionEpoch = UUID()
            api?.invalidate(); api = nil
            authenticated = false; isAdmin = false
            conversations = []; models = []; messages = []; conversation = nil
            for file in pendingFiles + fileCache.values.flatMap({ $0 }) { try? FileManager.default.removeItem(at: file.url) }
            pendingFiles = []; fileCache = [:]; draftCache = [:]; draft = ""; stream = StreamPresentation()
            isStreaming = false; needsReconnect = false; personaID = ""; error = nil
        } catch { report(error) }
    }

    private func saveDraft() {
        let key = conversation?.id ?? "new"
        draftCache[key] = draft; fileCache[key] = pendingFiles
    }

    func newChat() {
        saveDraft(); streamTask?.cancel(); selectionEpoch = UUID()
        conversation = nil; messages = []; stream = StreamPresentation()
        isStreaming = false; needsReconnect = false; hasOlderHistory = false
        draft = draftCache["new"] ?? ""; pendingFiles = fileCache["new"] ?? []
        restorePersona()
        error = nil
    }

    func open(_ selected: Conversation) async {
        guard let api else { return }
        saveDraft(); streamTask?.cancel()
        let epoch = UUID(); selectionEpoch = epoch
        conversation = selected; mode = selected.mode
        restorePersona()
        reasoningEffort = selected.reasoningEffort
        selectedModelID = models.first { $0.model == selected.model && $0.endpointURL == selected.endpointURL }?.id ?? ""
        draft = draftCache[selected.id] ?? ""; pendingFiles = fileCache[selected.id] ?? []
        isStreaming = false; needsReconnect = false; messages = []; stream = StreamPresentation()
        busy = true; error = nil
        defer { if selectionEpoch == epoch { busy = false } }
        do {
            let history = try await api.request("api/history/\(selected.id.pathComponent)?limit=50")
            guard selectionEpoch == epoch else { return }
            applyHistory(history)
            do {
                _ = try await api.request("api/chat/stream_status/\(selected.id.pathComponent)")
                guard selectionEpoch == epoch else { return }
                // Resume replays the whole run. Persisted intermediate rounds must not appear twice.
                while !messages.contains(where: { $0.role == "user" }) && hasOlderHistory {
                    let previousOffset = historyOffset
                    await loadOlder()
                    guard selectionEpoch == epoch else { return }
                    if historyOffset == previousOffset { break }
                }
                if let lastUser = messages.lastIndex(where: { $0.role == "user" }) { messages = Array(messages[...lastUser]) }
                beginStream(path: "api/chat/resume/\(selected.id.pathComponent)", sessionID: selected.id, epoch: epoch)
            } catch ClientError.http(404, _) { needsReconnect = false }
        } catch { if selectionEpoch == epoch { report(error); needsReconnect = true } }
    }

    private func applyHistory(_ history: JSONValue) {
        messages = ChatEntry.history(history)
        stream.approval = messages.last?.approval ?? .null
        reasoningEffort = history["reasoning_effort"].string
        historyOffset = Int(history["offset"].number ?? 0)
        hasOlderHistory = history["has_more_before"].bool
        if var current = conversation {
            current.model = history["model"].string
            current.endpointURL = history["endpoint_url"].string
            if !history["name"].string.isEmpty { current.name = history["name"].string }
            conversation = current
            selectedModelID = models.first { $0.model == current.model && $0.endpointURL == current.endpointURL }?.id ?? ""
        }
    }

    func loadOlder() async {
        guard let api, let conversation, historyOffset > 0 else { return }
        let epoch = selectionEpoch
        do {
            let offset = max(0, historyOffset - 50)
            let page = try await api.request("api/history/\(conversation.id.pathComponent)?limit=\(historyOffset - offset)&offset=\(offset)")
            guard selectionEpoch == epoch else { return }
            let ids = Set(messages.map(\.id))
            messages.insert(contentsOf: ChatEntry.history(page).filter { !ids.contains($0.id) }, at: 0)
            historyOffset = offset; hasOlderHistory = offset > 0
        } catch { report(error) }
    }

    func changeModel(_ model: ModelChoice) async {
        guard !isStreaming, let api else { return }
        do {
            if let current = conversation {
                _ = try await api.request("api/session/\(current.id.pathComponent)", method: "PATCH", form: ["model": model.model, "endpoint_url": model.endpointURL, "endpoint_id": model.endpointID, "reasoning_effort": ""])
                conversation?.model = model.model; conversation?.endpointURL = model.endpointURL
            }
            selectedModelID = model.id
            reasoningEffort = ""
        } catch { report(error) }
    }

    func setReasoning(_ effort: String, modelID: String) async throws {
        guard modelID == selectedModelID, let api else { throw ClientError.message("The selected model changed. Reopen its controls.") }
        if let conversation {
            _ = try await api.request("api/session/\(conversation.id.pathComponent)", method: "PATCH", form: ["reasoning_effort": effort])
        }
        reasoningEffort = effort
    }

    func addFile(_ url: URL) throws {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let copy = folder.appendingPathComponent(url.lastPathComponent)
        try FileManager.default.copyItem(at: url, to: copy)
        pendingFiles.append(PendingFile(url: copy, name: url.lastPathComponent,
                                       mime: UTType(filenameExtension: url.pathExtension)?.preferredMIMEType ?? "application/octet-stream"))
    }

    func removeFile(_ id: UUID) {
        if let file = pendingFiles.first(where: { $0.id == id }) { try? FileManager.default.removeItem(at: file.url) }
        pendingFiles.removeAll { $0.id == id }
    }

    func submit(approval: JSONValue? = nil, decision: String? = nil) {
        guard !busy, !isStreaming else { return }
        sendTask = Task { [weak self] in await self?.send(approval: approval, decision: decision) }
    }

    func cancelUpload() { sendTask?.cancel() }

    func send(approval: JSONValue? = nil, decision: String? = nil) async {
        guard let api, !busy, !isStreaming, !needsReconnect else { return }
        let originalDraft = draft
        let text = approval == nil ? originalDraft.trimmingCharacters(in: .whitespacesAndNewlines) : ""
        guard approval != nil || !text.isEmpty || !pendingFiles.isEmpty else { return }
        guard let model = selectedModel else { error = "Select an available model first."; return }
        busy = true; error = nil
        defer { busy = false; uploadStatus = "" }
        let epoch = selectionEpoch
        do {
            if conversation == nil {
                let persona = personaID
                let created = try await api.request("api/session", method: "POST", form: ["name": "", "endpoint_id": model.endpointID, "endpoint_url": model.endpointURL, "model": model.model, "reasoning_effort": reasoningEffort])
                conversation = Conversation(created)
                conversation?.endpointURL = model.endpointURL; conversation?.model = model.model
                selectPersona(persona)
                UserDefaults.standard.removeObject(forKey: personaKey("new"))
                draftCache.removeValue(forKey: "new"); fileCache.removeValue(forKey: "new")
            }
            try Task.checkCancellation()
            guard let id = conversation?.id, !id.isEmpty else { throw ClientError.message("Could not create the conversation.") }
            if approval == nil {
                for index in pendingFiles.indices where pendingFiles[index].uploaded == nil {
                    uploadStatus = "Uploading \(pendingFiles[index].name)…"
                    let fileID = pendingFiles[index].id
                    let uploaded = try await api.upload(pendingFiles[index], sessionID: id)
                    if let current = pendingFiles.firstIndex(where: { $0.id == fileID }) { pendingFiles[current].uploaded = uploaded }
                }
            }
            try Task.checkCancellation()
            guard epoch == selectionEpoch else { return }
            var fields = ["session": id, "message": text, "mode": mode,
                          "plan_mode": String(planMode), "allow_bash": String(allowShell),
                          "allow_web_search": String(allowWeb)]
            if URL(string: model.endpointURL)?.host == "chatgpt.com" { fields["reasoning_effort"] = reasoningEffort }
            if !personaID.isEmpty { fields["preset_id"] = personaID }
            let files = approval == nil ? pendingFiles.compactMap(\.uploaded) : []
            if !files.isEmpty { fields["attachments"] = JSONValue.array(files.map { .string($0.id) }).text }
            if let approval, let decision {
                fields["tool_approval_id"] = approval["approval_id"].string
                fields["tool_approval_decision"] = decision
                fields["mode"] = "agent"
            } else { messages.append(ChatEntry(id: UUID().uuidString, role: "user", text: text, attachments: files)) }
            beginStream(path: "api/chat_stream", fields: fields, sessionID: id, epoch: epoch,
                        sentDraft: approval == nil ? originalDraft : nil,
                        sentFileIDs: approval == nil ? Set(pendingFiles.map(\.id)) : [])
        } catch { report(error) }
    }

    private func beginStream(path: String, fields: [String: String]? = nil, sessionID: String, epoch: UUID, sentDraft: String? = nil, sentFileIDs: Set<UUID> = []) {
        guard let api else { return }
        stream = StreamPresentation(); isStreaming = true; needsReconnect = false
        streamTask = Task { [weak self] in
            guard let self else { return }
            defer { if self.selectionEpoch == epoch { self.isStreaming = false } }
            do {
                try await api.stream(path: path, fields: fields, started: { runID in
                    guard self.selectionEpoch == epoch else { return }
                    self.stream.runID = runID
                    if let sentDraft {
                        if self.draft == sentDraft { self.draft = "" }
                        for file in self.pendingFiles where sentFileIDs.contains(file.id) { try? FileManager.default.removeItem(at: file.url) }
                        self.pendingFiles.removeAll { sentFileIDs.contains($0.id) }; self.saveDraft()
                    }
                }, receive: { event in
                    if self.selectionEpoch == epoch { self.stream.receive(event) }
                })
                try Task.checkCancellation()
                guard self.selectionEpoch == epoch else { return }
                let approval = self.stream.approval
                let history = try await api.request("api/history/\(sessionID.pathComponent)?limit=50")
                guard self.selectionEpoch == epoch else { return }
                self.applyHistory(history)
                let pending = approval.isNull ? self.stream.approval : approval
                self.stream = StreamPresentation(); self.stream.approval = pending
                try await self.refresh()
            } catch {
                guard self.selectionEpoch == epoch else { return }
                if case ClientError.http(404, _) = error, fields == nil {
                    do {
                        let history = try await api.request("api/history/\(sessionID.pathComponent)?limit=50")
                        guard self.selectionEpoch == epoch else { return }
                        self.applyHistory(history)
                    }
                    catch { self.report(error) }
                } else { self.report(error); self.needsReconnect = true }
            }
        }
    }

    func stop() async {
        guard let api, let conversation else { return }
        do {
            if try await api.stop(sessionID: conversation.id, runID: stream.runID) { stream.status = "Stopping…" }
            else { needsReconnect = true; error = "That run already ended or changed. Reconnect to load its current state." }
        } catch { report(error) }
    }

    func suspend() { if isStreaming { streamTask?.cancel(); needsReconnect = true } }
    func resume() async { if authenticated, !busy, let conversation { await open(conversation) } }
}

import SwiftUI

enum OdysseusTheme {
    static let background = Color(red: 40 / 255, green: 44 / 255, blue: 52 / 255)
    static let panel = Color(red: 17 / 255, green: 17 / 255, blue: 17 / 255)
    static let foreground = Color(red: 156 / 255, green: 222 / 255, blue: 242 / 255)
    static let border = Color(red: 53 / 255, green: 90 / 255, blue: 102 / 255)
    static let coral = Color(red: 224 / 255, green: 108 / 255, blue: 117 / 255)
}

enum AppSheet: Identifiable {
    case history, models, controls(ModelChoice), persona, connection
    var id: String {
        switch self { case .history: return "history"; case .models: return "models"; case .controls(let model): return model.id; case .persona: return "persona"; case .connection: return "connection" }
    }
}

struct RootView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.horizontalSizeClass) private var sizeClass
    @State private var sheet: AppSheet?

    var body: some View {
        Group {
            if store.authenticated {
                if sizeClass == .regular {
                    NavigationSplitView {
                        ConversationList { conversation in Task { await store.open(conversation) } } newChat: { store.newChat() }
                    } detail: { chat }
                } else {
                    NavigationStack { chat }
                }
            } else { ConnectionView() }
        }
        .sheet(item: $sheet) { destination in
            switch destination {
            case .history:
                NavigationStack {
                    ConversationList { conversation in
                        sheet = nil; Task { await store.open(conversation) }
                    } newChat: { store.newChat(); sheet = nil }
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { sheet = nil } } }
                }
            case .models: ModelPickerView()
            case .controls(let model): ModelControlsView(model: model)
            case .persona: PersonaView()
            case .connection: AccountView()
            }
        }
    }

    private var chat: some View {
        ChatView(sheet: $sheet)
            .navigationTitle(store.conversation?.name ?? "Odysseus")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if sizeClass != .regular {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { sheet = .history } label: { Image(systemName: "sidebar.left") }
                            .accessibilityLabel("Conversations").accessibilityIdentifier("conversationsButton")
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { store.newChat() } label: { Image(systemName: "square.and.pencil") }
                        .accessibilityLabel("New chat").disabled(store.busy)
                    Button { sheet = .connection } label: { Image(systemName: "person.crop.circle") }
                        .accessibilityLabel("Connection and account")
                }
            }
    }
}

struct ConversationList: View {
    @Environment(AppStore.self) private var store
    let select: (Conversation) -> Void
    let newChat: () -> Void
    @State private var query = ""
    private var filtered: [Conversation] {
        store.conversations.filter { query.isEmpty || $0.name.localizedCaseInsensitiveContains(query) || $0.model.localizedCaseInsensitiveContains(query) }
    }
    var body: some View {
        List {
            Section {
                Button(action: newChat) { Label("New Chat", systemImage: "square.and.pencil") }.disabled(store.busy)
            }
            Section("Conversations") {
                ForEach(filtered) { item in
                    Button { select(item) } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.name).foregroundStyle(.primary).lineLimit(2)
                            Text(item.mode == "agent" ? "Agent" : "Chat").font(.caption).foregroundStyle(.secondary)
                        }.padding(.vertical, 4)
                    }.disabled(store.busy)
                }
                if filtered.isEmpty { Text(query.isEmpty ? "Your conversations will appear here." : "No matching conversations.").foregroundStyle(.secondary) }
            }
        }
        .navigationTitle("Odysseus")
        .searchable(text: $query, prompt: "Search conversations")
        .scrollContentBackground(.hidden).background(OdysseusTheme.background)
        .refreshable { do { try await store.refresh() } catch { store.report(error) } }
    }
}

struct ConnectionView: View {
    @Environment(AppStore.self) private var store
    @State private var password = ""
    @State private var code = ""
    @State private var remember = true
    var body: some View {
        @Bindable var store = store
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        if let path = Bundle.main.path(forResource: "OdysseusIcon", ofType: "png"), let icon = UIImage(contentsOfFile: path) {
                            Image(uiImage: icon).resizable().scaledToFit().frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 14))
                        }
                        Text("Odysseus").font(.largeTitle.weight(.semibold)).foregroundStyle(OdysseusTheme.coral)
                        Text("Connect to your desktop.").font(.title3)
                        Text("Your conversations, models, and tools stay on your Odysseus server.").font(.callout).foregroundStyle(.secondary)
                    }.padding(.vertical, 20)
                }.listRowBackground(Color.clear)
                Section("Desktop server") {
                    TextField("https://your-desktop", text: $store.serverAddress)
                        .keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityIdentifier("serverAddress")
                    Text("Use the Odysseus application address reachable from this device.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Account") {
                    TextField("Username", text: $store.username).textContentType(.username).textInputAutocapitalization(.never).autocorrectionDisabled().accessibilityIdentifier("username")
                    SecureField("Password", text: $password).textContentType(.password).accessibilityIdentifier("password")
                    if store.requiresTOTP {
                        TextField("Authenticator code", text: $code).textContentType(.oneTimeCode).keyboardType(.numberPad).accessibilityIdentifier("totpCode")
                    }
                    Toggle("Remember this connection", isOn: $remember)
                }
                if let error = store.error { Section { Text(error).foregroundStyle(OdysseusTheme.coral).accessibilityIdentifier("connectionError") } }
                Section {
                    Button {
                        Task { await store.connect(password: password, code: code, remember: remember); if store.authenticated { password = ""; code = "" } }
                    } label: {
                        HStack { Spacer(); if store.busy { ProgressView() }; Text(store.busy ? "Connecting…" : "Connect"); Spacer() }
                    }.disabled(store.busy || store.serverAddress.isEmpty).accessibilityIdentifier("connectButton")
                }
            }
            .scrollContentBackground(.hidden).background(OdysseusTheme.background)
            .navigationTitle("Welcome").navigationBarTitleDisplayMode(.inline)
        }
    }
}

struct AccountView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                Section("Connected server") { Text(store.serverLabel).textSelection(.enabled); Label(store.username, systemImage: "person.crop.circle") }
                Section { Text("Models, files, and tools run on this desktop server. Closing the app leaves an active run on the server.").font(.callout).foregroundStyle(.secondary) }
                Section {
                    Button("Sign out", role: .destructive) { Task { await store.logout(); if !store.authenticated { dismiss() } } }.disabled(store.busy)
                }
                if let error = store.error { Text(error).foregroundStyle(OdysseusTheme.coral) }
            }
            .scrollContentBackground(.hidden).background(OdysseusTheme.background)
            .navigationTitle("Connection").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }
}

#Preview { RootView().environment(AppStore()).preferredColorScheme(.dark) }

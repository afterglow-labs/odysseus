import SwiftUI

struct PersonaView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var templates: [JSONValue] = []
    @State private var original: JSONValue = .null
    @State private var saveTemplate = true
    @State private var loading = true
    @State private var saving = false
    @State private var loaded = false
    @State private var status = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Give Odysseus an identity, personality, and instructions for this conversation.").font(.callout).foregroundStyle(.secondary)
                    if !templates.isEmpty {
                        Menu("Load saved persona") {
                            ForEach(Array(templates.enumerated()), id: \.offset) { _, item in
                                Button(item["name"].string) { name = item["name"].string; prompt = item["system_prompt"].string }
                            }
                        }
                    }
                }
                Section("Identity") {
                    TextField("Name", text: $name).accessibilityIdentifier("personaName")
                    TextEditor(text: $prompt).frame(minHeight: 220).accessibilityLabel("Personality and system instructions").accessibilityIdentifier("personaPrompt")
                }.disabled(!store.isAdmin)
                Section {
                    Toggle("Save as reusable template", isOn: $saveTemplate).disabled(!store.isAdmin)
                    if !store.personaID.isEmpty {
                        Button("Use default persona in this chat") { store.selectPersona(""); status = "Default persona selected for the next message." }
                    }
                    if !store.isAdmin { Text("Editing server personas requires an administrator account.").font(.caption).foregroundStyle(.secondary) }
                }
                if loading { ProgressView("Loading personas…") }
                if !status.isEmpty { Text(status).font(.callout).accessibilityIdentifier("personaStatus") }
            }
            .disabled(loading || saving)
            .scrollContentBackground(.hidden).background(OdysseusTheme.background)
            .navigationTitle("Persona").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(saving ? "Saving…" : "Save & use") { Task { await save() } }.disabled(!loaded || saving || !store.isAdmin || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty).accessibilityIdentifier("savePersona") }
            }
            .task { await load() }
        }
    }

    private func load() async {
        defer { loading = false }
        do {
            guard let api = store.api else { return }
            let presets = try await api.request("api/presets")
            original = presets["custom"]
            name = original["character_name"].string.isEmpty ? original["name"].string : original["character_name"].string
            prompt = original["system_prompt"].string
            templates = try await api.request("api/presets/templates").array
            loaded = true
        } catch { status = error.localizedDescription }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            guard let api = store.api else { return }
            let temp = original["temperature"].number ?? 1
            let tokens = original["max_tokens"].number ?? 0
            let result = try await api.request("api/presets/custom", method: "POST", json: .object([
                "name": .string(name), "system_prompt": .string(prompt), "enabled": .bool(true),
                "temperature": .number(temp), "max_tokens": .number(tokens),
                "inject_prefix": .string(original["inject_prefix"].string), "inject_suffix": .string(original["inject_suffix"].string)
            ]))
            guard result["success"].bool else { throw ClientError.message("The server could not save this persona.") }
            store.selectPersona("custom")
            if saveTemplate {
                let existing = templates.first { $0["name"].string == name }
                let template = try await api.request("api/presets/templates", method: "POST", json: .object([
                    "id": .string(existing?["id"].string ?? ""), "name": .string(name), "system_prompt": .string(prompt),
                    "temperature": .number(temp), "max_tokens": .number(tokens)
                ]))
                guard template["success"].bool else { throw ClientError.message("Persona activated, but the reusable template could not be saved.") }
            }
            status = "Saved and active for the next message in Chat or Agent mode."
        } catch { status = error.localizedDescription }
    }
}

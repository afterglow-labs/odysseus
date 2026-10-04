import SwiftUI

struct ModelPickerView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    var body: some View {
        NavigationStack {
            List {
                ForEach(store.models.filter { query.isEmpty || $0.label.localizedCaseInsensitiveContains(query) || $0.model.localizedCaseInsensitiveContains(query) || $0.endpointName.localizedCaseInsensitiveContains(query) }) { model in
                    Button {
                        Task { await store.changeModel(model); if store.selectedModelID == model.id { dismiss() } }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 6) { Text(model.label).foregroundStyle(.primary); Text(model.endpointName).font(.caption).foregroundStyle(.secondary) }
                            Spacer(); if store.selectedModelID == model.id { Image(systemName: "checkmark") }
                        }.padding(.vertical, 6)
                    }
                }
                if store.models.isEmpty { Text("No models are available on this server. Configure or launch one in the desktop app.").foregroundStyle(.secondary) }
            }
            .searchable(text: $query, prompt: "Search models")
            .refreshable { do { try await store.refresh() } catch { store.report(error) } }
            .scrollContentBackground(.hidden).background(OdysseusTheme.background)
            .navigationTitle("Models").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
    }
}

struct ModelControlsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let model: ModelChoice
    @State private var values: [String: String] = [:]
    @State private var loading = true
    @State private var saving = false
    @State private var loaded = false
    @State private var status = ""
    @State private var failed = false

    private let common = [("temperature", "Temperature"), ("max_tokens", "Max output tokens"), ("top_p", "Top-p"), ("top_k", "Top-k"), ("min_p", "Min-p"), ("seed", "Seed"), ("reasoning_budget", "Thinking token budget"), ("repeat_penalty", "Repetition penalty"), ("presence_penalty", "Presence penalty"), ("frequency_penalty", "Frequency penalty")]
    private let advanced = [("typical_p", "Typical-p"), ("repeat_last_n", "Repetition window"), ("mirostat", "Mirostat"), ("mirostat_tau", "Mirostat tau"), ("mirostat_eta", "Mirostat eta"), ("dynatemp_range", "Dynamic temperature range"), ("dynatemp_exponent", "Dynamic temperature exponent"), ("xtc_probability", "XTC probability"), ("xtc_threshold", "XTC threshold"), ("dry_multiplier", "DRY multiplier"), ("dry_base", "DRY base"), ("dry_allowed_length", "DRY allowed length"), ("dry_penalty_last_n", "DRY window")]

    private func binding(_ key: String) -> Binding<String> { Binding(get: { values[key] ?? "" }, set: { values[key] = $0 }) }
    private var subscriptionModel: Bool { URL(string: model.endpointURL)?.host == "chatgpt.com" }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(model.label).font(.headline)
                    Text(model.endpointName).font(.caption).foregroundStyle(.secondary)
                    Text("Saved on your desktop for this model. Blank fields use the existing defaults.").font(.caption).foregroundStyle(.secondary)
                    if !status.isEmpty { Text(status).foregroundStyle(failed ? OdysseusTheme.coral : .secondary).accessibilityIdentifier("controlsStatus") }
                }
                if subscriptionModel {
                    Section("Reasoning effort") {
                        Picker("Effort", selection: binding("reasoning_effort")) {
                            Text(model.defaultReasoning.isEmpty ? "Provider default" : "Provider default (\(model.defaultReasoning.capitalized))").tag("")
                            ForEach(model.reasoningLevels, id: \.self) { Text($0.capitalized).tag($0) }
                            if let saved = values["reasoning_effort"], !saved.isEmpty, !model.reasoningLevels.contains(saved) { Text("\(saved) (saved)").tag(saved) }
                        }
                        Text(model.reasoningLevels.isEmpty ? "The server has not advertised adjustable reasoning levels. You can keep the saved setting or reset to provider default." : "Choose how much reasoning this model uses. Higher effort may take longer.").font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    Section("Reasoning") {
                        Picker("Thinking", selection: binding("thinking")) { Text("Model default").tag(""); Text("Off").tag("off"); Text("On").tag("on") }.accessibilityIdentifier("thinkingPicker")
                        Picker("Reasoning effort", selection: binding("reasoning_effort")) {
                            Text("Model default").tag("")
                            ForEach(["none", "minimal", "low", "medium", "high", "xhigh", "max"], id: \.self) { Text($0 == "none" ? "None (thinking off)" : $0.capitalized).tag($0) }
                        }
                    }
                    Section("Generation") { numericRows(common) }
                    Section {
                        DisclosureGroup("Advanced sampling") { numericRows(advanced) }
                    } footer: { Text("Availability depends on the serving engine. GPU allocation, context capacity, and MTP remain launch settings on the desktop.") }
                    Section("Stop sequences") { TextField("One per line", text: binding("stop"), axis: .vertical).lineLimit(3...8).autocorrectionDisabled().textInputAutocapitalization(.never) }
                    Section("Sampler order") { TextField("One per line", text: binding("samplers"), axis: .vertical).lineLimit(2...8).autocorrectionDisabled().textInputAutocapitalization(.never) }
                    Section { Button("Reset fields") { values = [:]; status = "Press Save to restore defaults." } }
                }
                if loading { ProgressView("Loading controls…") }
            }
            .disabled(loading || saving)
            .scrollContentBackground(.hidden).background(OdysseusTheme.background)
            .navigationTitle("Model controls").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(saving ? "Saving…" : "Save") { Task { await save() } }.disabled(!loaded || saving).accessibilityIdentifier("saveControls") }
            }
            .task { await load() }
        }
    }

    @ViewBuilder private func numericRows(_ fields: [(String, String)]) -> some View {
        ForEach(fields, id: \.0) { key, label in
            HStack {
                Text(label).font(.callout)
                Spacer()
                TextField("Default", text: binding(key)).multilineTextAlignment(.trailing).keyboardType(.numbersAndPunctuation)
                    .frame(maxWidth: 125).accessibilityLabel(label).accessibilityIdentifier(key)
            }
        }
    }

    private func load() async {
        defer { loading = false }
        do {
            if subscriptionModel { values["reasoning_effort"] = store.reasoningEffort; loaded = true; return }
            guard let api = store.api else { return }
            let result = try await api.request("api/prefs/model-generation")
            fill(result["value"][model.preferenceKey]); loaded = true
        } catch { failed = true; status = error.localizedDescription }
    }

    private func fill(_ options: JSONValue) {
        values = options.object.mapValues { value in value.array.isEmpty ? value.text : value.array.map(\.text).joined(separator: "\n") }
        if values["thinking"] == "auto" { values["thinking"] = "" }
        if values["reasoning_effort"] == "default" { values["reasoning_effort"] = "" }
    }

    private func save() async {
        saving = true; failed = false
        defer { saving = false }
        do {
            if subscriptionModel {
                try await store.setReasoning(values["reasoning_effort"] ?? "", modelID: model.id)
                status = "Saved. Applies to the next message with this model."
                return
            }
            var options: [String: JSONValue] = [:]
            for (key, text) in values where !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                if ["thinking", "reasoning_effort"].contains(key) { options[key] = .string(text) }
                else if ["stop", "samplers"].contains(key) { options[key] = .array(text.components(separatedBy: "\n").filter { !$0.isEmpty }.map(JSONValue.string)) }
                else if let number = Double(text), number.isFinite { options[key] = .number(number) }
                else { throw ClientError.message("Enter a number for \(key).") }
            }
            guard let api = store.api else { return }
            let result = try await api.request("api/prefs/model-generation", method: "PUT", json: .object(["model_key": .string(model.preferenceKey), "options": .object(options)]))
            fill(result["value"][model.preferenceKey]); status = "Saved. Applies to the next message with this model."
        } catch { failed = true; status = error.localizedDescription }
    }
}

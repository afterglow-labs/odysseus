import SwiftUI

@main
struct OdysseusApp: App {
    @State private var store = AppStore()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            RootView().environment(store).tint(OdysseusTheme.coral)
                .font(.custom("FiraCode-Regular", size: 15, relativeTo: .body))
                .preferredColorScheme(.dark)
                .task { await store.restore() }
                .onChange(of: phase) { old, new in
                    if new == .background { store.suspend() }
                    else if old == .background && new == .active { Task { await store.resume() } }
                }
        }
    }
}

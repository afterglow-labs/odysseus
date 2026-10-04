import Foundation
import Security

struct SessionCredential: Codable {
    let value: String
    let secure: Bool
    let expires: Date?
    var valid: Bool { expires.map { $0 > Date() } ?? true }
}

enum CredentialVault {
    private static func query(_ server: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "dev.afterglow.odysseus.session",
         kSecAttrAccount as String: server]
    }

    static func load(server: String) -> SessionCredential? {
        var request = query(server)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let credential = try? JSONDecoder().decode(SessionCredential.self, from: data), credential.valid else { return nil }
        return credential
    }

    static func save(_ credential: SessionCredential?, server: String) throws {
        let request = query(server)
        guard let credential else { SecItemDelete(request as CFDictionary); return }
        let data = try JSONEncoder().encode(credential)
        let attributes: [String: Any] = [kSecValueData as String: data,
                                       kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(request as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(request.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw ClientError.message("Could not securely save the session (Keychain \(status)).") }
    }
}

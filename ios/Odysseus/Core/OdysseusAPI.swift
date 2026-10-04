import Foundation

private final class SameServerRedirects: NSObject, URLSessionTaskDelegate {
    let server: URL
    init(server: URL) { self.server = server }
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        let url = request.url
        let same = url?.scheme == server.scheme && url?.host == server.host && url?.port == server.port
        completionHandler(same ? request : nil)
    }
}

@MainActor
final class OdysseusAPI {
    let baseURL: URL
    private let session: URLSession
    private var credential: SessionCredential?
    private var remember = true

    static func normalizeServer(_ address: String) throws -> URL {
        let text = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: text), ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil else {
            throw ClientError.message("Enter the Odysseus server URL, including https:// or http:// and its port if needed.")
        }
        parts.scheme = parts.scheme?.lowercased()
        parts.host = host.lowercased()
        while parts.path.hasSuffix("/") { parts.path.removeLast() }
        guard let url = parts.url else { throw ClientError.message("Invalid server address.") }
        return url
    }

    init(address: String, configuration: URLSessionConfiguration = .ephemeral) throws {
        baseURL = try Self.normalizeServer(address)
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 180
        configuration.timeoutIntervalForResource = 3600
        session = URLSession(configuration: configuration, delegate: SameServerRedirects(server: baseURL), delegateQueue: nil)
        credential = CredentialVault.load(server: baseURL.absoluteString)
    }

    func invalidate() { session.invalidateAndCancel() }

    private func makeRequest(_ path: String, method: String = "GET") throws -> URLRequest {
        guard let url = URL(string: baseURL.absoluteString + "/" + path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))),
              url.host == baseURL.host, url.scheme == baseURL.scheme, url.port == baseURL.port else {
            throw ClientError.message("Invalid API path.")
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let credential, credential.valid, !credential.secure || url.scheme == "https" {
            request.setValue("odysseus_session=\(credential.value)", forHTTPHeaderField: "Cookie")
        }
        return request
    }

    private func validate(_ response: URLResponse, data: Data = Data()) throws -> HTTPURLResponse {
        guard let http = response as? HTTPURLResponse else { throw ClientError.message("The server returned an invalid response.") }
        if http.statusCode == 401 {
            credential = nil
            try CredentialVault.save(nil, server: baseURL.absoluteString)
        }
        guard (200..<300).contains(http.statusCode) else {
            let error = (try? JSONDecoder().decode(JSONValue.self, from: data)) ?? .null
            let detail = error["detail"].isNull ? error["error"].text : error["detail"].text
            throw ClientError.http(http.statusCode, detail.isEmpty ? HTTPURLResponse.localizedString(forStatusCode: http.statusCode) : detail)
        }
        let headers = http.allHeaderFields.reduce(into: [String: String]()) { result, pair in result[String(describing: pair.key)] = String(describing: pair.value) }
        for cookie in HTTPCookie.cookies(withResponseHeaderFields: headers, for: baseURL) where cookie.name == "odysseus_session" {
            credential = SessionCredential(value: cookie.value, secure: cookie.isSecure, expires: cookie.expiresDate)
            if remember { try CredentialVault.save(credential, server: baseURL.absoluteString) }
        }
        return http
    }

    func request(_ path: String, method: String = "GET", json: JSONValue? = nil, form: [String: String]? = nil) async throws -> JSONValue {
        var request = try makeRequest(path, method: method)
        if let json {
            request.httpBody = try json.encoded()
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        } else if let form {
            let boundary = "Odysseus-" + UUID().uuidString
            request.httpBody = Self.formData(form, boundary: boundary)
            request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        _ = try validate(response, data: data)
        if data.isEmpty { return .null }
        do { return try JSONDecoder().decode(JSONValue.self, from: data) }
        catch { throw ClientError.message("This address did not return an Odysseus API response. Check the application URL and port.") }
    }

    func login(username: String, password: String, code: String, remember: Bool) async throws -> JSONValue {
        self.remember = remember
        if !remember { try CredentialVault.save(nil, server: baseURL.absoluteString) }
        let reply = try await request("api/auth/login", method: "POST", json: .object([
            "username": .string(username), "password": .string(password), "totp_code": .string(code), "remember": .bool(remember)
        ]))
        if reply["requires_totp"].bool { throw ClientError.requiresTOTP }
        guard reply["ok"].bool else { throw ClientError.message("Sign-in was not accepted.") }
        return try await request("api/auth/status")
    }

    func logout() async throws {
        _ = try await request("api/auth/logout", method: "POST")
        credential = nil
        try CredentialVault.save(nil, server: baseURL.absoluteString)
    }

    func stream(path: String, fields: [String: String]? = nil,
                started: (String) -> Void, receive: (JSONValue) -> Void) async throws {
        var request = try makeRequest(path, method: fields == nil ? "GET" : "POST")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(TimeZone.current.identifier, forHTTPHeaderField: "X-Tz-Name")
        request.setValue(String(TimeZone.current.secondsFromGMT() / 60), forHTTPHeaderField: "X-Tz-Offset")
        if let fields {
            let boundary = "Odysseus-" + UUID().uuidString
            request.httpBody = Self.formData(fields, boundary: boundary)
            request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        }
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse else { throw ClientError.message("Invalid stream response.") }
        if !(200..<300).contains(http.statusCode) {
            var body = Data()
            for try await byte in bytes { body.append(byte); if body.count > 65536 { break } }
            _ = try validate(response, data: body)
        }
        _ = try validate(response)
        guard http.value(forHTTPHeaderField: "Content-Type")?.contains("text/event-stream") == true else {
            throw ClientError.message("The server did not return a chat stream.")
        }
        started(http.value(forHTTPHeaderField: "X-Odysseus-Run-Id") ?? "")
        var parser = SSEParser()
        var chunk = Data()
        func deliver(_ frames: [SSEFrame]) throws -> Bool {
            for frame in frames {
                if frame.data == "[DONE]" { return true }
                let event = try JSONDecoder().decode(JSONValue.self, from: Data(frame.data.utf8))
                if frame.event == "error" || (event["status"].number ?? 0) >= 400 {
                    throw ClientError.message(event["error"].text.isEmpty ? event.text : event["error"].text)
                }
                receive(event)
            }
            return false
        }
        // AsyncBytes.lines can discard empty lines; SSE needs them as frame boundaries.
        for try await byte in bytes {
            try Task.checkCancellation()
            chunk.append(byte)
            if byte == 10 || chunk.count >= 8192 {
                if try deliver(parser.feed(chunk)) { return }
                chunk.removeAll(keepingCapacity: true)
            }
        }
        if try deliver(parser.feed(chunk)) || deliver(parser.finish()) { return }
        throw ClientError.message("Connection ended before completion. Reconnect to check the saved response.")
    }

    func stop(sessionID: String, runID: String) async throws -> Bool {
        guard !runID.isEmpty else { throw ClientError.message("Waiting for the server's run identity. Reconnect before stopping.") }
        var request = try makeRequest("api/chat/stop/\(sessionID.pathComponent)", method: "POST")
        request.setValue(runID, forHTTPHeaderField: "X-Odysseus-Run-Id")
        let (data, response) = try await session.data(for: request)
        _ = try validate(response, data: data)
        return try JSONDecoder().decode(JSONValue.self, from: data)["stopped"].bool
    }

    func upload(_ file: PendingFile, sessionID: String?) async throws -> Attachment {
        let boundary = "Odysseus-" + UUID().uuidString
        let bodyURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        FileManager.default.createFile(atPath: bodyURL.path, contents: nil)
        defer { try? FileManager.default.removeItem(at: bodyURL) }
        let output = try FileHandle(forWritingTo: bodyURL)
        defer { try? output.close() }
        var fields: [String: String] = [:]
        if let sessionID { fields["session_id"] = sessionID }
        try output.write(contentsOf: Self.formData(fields, boundary: boundary, close: false))
        let filename = file.name.replacingOccurrences(of: "\"", with: "_").replacingOccurrences(of: "\r", with: "_").replacingOccurrences(of: "\n", with: "_")
        try output.write(contentsOf: Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"files\"; filename=\"\(filename)\"\r\nContent-Type: \(file.mime)\r\n\r\n".utf8))
        let input = try FileHandle(forReadingFrom: file.url)
        defer { try? input.close() }
        while let chunk = try input.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            try Task.checkCancellation()
            try output.write(contentsOf: chunk)
            await Task.yield()
        }
        try output.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
        try output.synchronize()
        var request = try makeRequest("api/upload", method: "POST")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.upload(for: request, fromFile: bodyURL)
        _ = try validate(response, data: data)
        let result = try JSONDecoder().decode(JSONValue.self, from: data)
        guard let item = result["files"].array.first, !item["id"].string.isEmpty else { throw ClientError.message("The file was not accepted. It is still attached so you can retry.") }
        return Attachment(item)
    }

    func download(_ attachment: Attachment) async throws -> URL {
        let request = try makeRequest("api/upload/\(attachment.id.pathComponent)")
        let (temporary, response) = try await session.download(for: request)
        _ = try validate(response)
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let target = directory.appendingPathComponent((attachment.name as NSString).lastPathComponent)
        try FileManager.default.moveItem(at: temporary, to: target)
        return target
    }

    static func formData(_ fields: [String: String], boundary: String, close: Bool = true) -> Data {
        var text = ""
        for key in fields.keys.sorted() {
            text += "--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(fields[key]!)\r\n"
        }
        if close { text += "--\(boundary)--\r\n" }
        return Data(text.utf8)
    }
}

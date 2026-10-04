import XCTest

final class ClientFlowTests: XCTestCase {
    private var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        let reset = expectation(description: "Reset fixture")
        var request = URLRequest(url: URL(string: "http://127.0.0.1:18766/test/reset")!)
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request) { _, response, error in
            XCTAssertNil(error)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
            reset.fulfill()
        }.resume()
        wait(for: [reset], timeout: 10)
        app = XCUIApplication()
        app.launchEnvironment["ODYSSEUS_TEST_SERVER"] = "http://127.0.0.1:18766"
        app.launchEnvironment["ODYSSEUS_TEST_RESET_AUTH"] = "1"
        addUIInterruptionMonitor(withDescription: "Password AutoFill") { alert in
            if alert.buttons["Not Now"].exists { alert.buttons["Not Now"].tap(); return true }
            return false
        }
        app.launch()
    }

    override func tearDownWithError() throws {
        if let app {
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = name
            screenshot.lifetime = .keepAlways
            add(screenshot)
        }
    }

    private func login() {
        let username = app.textFields["username"]
        XCTAssertTrue(username.waitForExistence(timeout: 15))
        username.tap(); username.typeText("tester")
        let password = app.secureTextFields["password"]
        password.tap(); password.typeText("fixture-password")
        app.buttons["connectButton"].tap()
        XCTAssertTrue(app.buttons["modelPickerButton"].waitForExistence(timeout: 15), app.debugDescription)
        if app.buttons["modelPickerButton"].isHittable { return }
        for bundle in ["com.apple.SafariViewService", "com.apple.springboard"] {
            let service = XCUIApplication(bundleIdentifier: bundle)
            guard service.state != .notRunning else { continue }
            let savePassword = service.buttons["Not Now"]
            if savePassword.exists { savePassword.tap(); break }
        }
    }

    private func send(_ text: String) {
        let composer = app.textFields["messageInput"].exists ? app.textFields["messageInput"] : app.textViews["messageInput"]
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        enter(text, into: composer)
        app.buttons["sendButton"].tap()
    }

    private func enter(_ text: String, into field: XCUIElement) {
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true AND hittable == true"), object: field)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 10), .completed)
        field.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), app.debugDescription)
        field.typeText(text)
    }

    func testLoginSendHistoryAndRememberedSession() {
        login()
        send("Hello from the native client")
        XCTAssertTrue(app.staticTexts["Your native client reached the desktop API."].waitForExistence(timeout: 15), app.debugDescription)
        let chat = XCTAttachment(screenshot: app.screenshot())
        chat.name = "Native streaming chat"
        chat.lifetime = .keepAlways
        add(chat)
        app.buttons["conversationsButton"].tap()
        XCTAssertTrue(app.buttons.containing(.staticText, identifier: "Desktop conversation").firstMatch.waitForExistence(timeout: 10))
        app.buttons.containing(.staticText, identifier: "Desktop conversation").firstMatch.tap()
        XCTAssertTrue(app.staticTexts["History loaded from the desktop."].waitForExistence(timeout: 10))
        app.terminate()
        app.launchEnvironment["ODYSSEUS_TEST_RESET_AUTH"] = "0"
        app.launch()
        XCTAssertTrue(app.buttons["modelPickerButton"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["connectButton"].exists)
    }

    func testControlsPersistAndStopCancelsTheActiveRun() {
        login()
        app.buttons["modelControlsButton"].tap()
        let temperature = app.textFields["temperature"]
        XCTAssertTrue(temperature.waitForExistence(timeout: 10))
        enter("0", into: temperature)
        app.buttons["saveControls"].tap()
        XCTAssertTrue(app.staticTexts["controlsStatus"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["controlsStatus"].label.contains("Saved"))
        app.buttons["Done"].tap()
        app.buttons["modelControlsButton"].tap()
        XCTAssertTrue(temperature.waitForExistence(timeout: 10))
        XCTAssertEqual(temperature.value as? String, "0")
        app.buttons["Done"].tap()
        send("slow response for cancellation")
        XCTAssertTrue(app.buttons["stopButton"].waitForExistence(timeout: 10))
        app.buttons["stopButton"].tap()
        XCTAssertTrue(app.buttons["sendButton"].waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertFalse(app.buttons["reconnectButton"].exists)
    }

    func testBadPasswordShowsServerError() {
        XCTAssertTrue(app.textFields["username"].waitForExistence(timeout: 15))
        app.textFields["username"].tap(); app.textFields["username"].typeText("tester")
        app.secureTextFields["password"].tap(); app.secureTextFields["password"].typeText("incorrect")
        app.buttons["connectButton"].tap()
        XCTAssertTrue(app.staticTexts["connectionError"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["connectionError"].label.contains("Invalid credentials"))
    }
}

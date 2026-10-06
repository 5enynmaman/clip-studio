import AppKit
import WebKit
import UniformTypeIdentifiers

@main
final class ClipStudioApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var runtime: Process?
    private var username = ""
    private var password = ""
    private var closing = false
    private var startupFailureShowing = false
    private var startupDeadline = Date.distantFuture
    private var storageVolumePath = ""
    private var storageImagePath = ""
    private var storageWaitTimer: Timer?
    private var storageDetailLabel: NSTextField?
    private var waitingForStorage = false

    static func main() {
        let app = NSApplication.shared
        let delegate = ClipStudioApp()
        app.delegate = delegate
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.applicationIconImage = NSImage(systemSymbolName: "film", accessibilityDescription: "Clip Studio")
        NSApp.mainMenu = makeMenu()

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.mediaTypesRequiringUserActionForPlayback = []
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Clip Studio"
        window.minSize = NSSize(width: 1100, height: 700)
        window.center()
        window.delegate = self
        window.contentView = webView
        showStatus("Запускаем Clip Studio…", detail: "Подготавливаем редактор и локальные службы.")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startRuntime()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let runtime, runtime.isRunning, !closing else { return .terminateNow }
        closing = true
        runtime.terminate()
        DispatchQueue.global(qos: .utility).async {
            runtime.waitUntilExit()
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    private func makeMenu() -> NSMenu {
        let menu = NSMenu()
        let appMenuItem = NSMenuItem()
        menu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "О Clip Studio", action: #selector(showAbout), keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Завершить Clip Studio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appMenuItem.submenu = appMenu

        let fileItem = NSMenuItem()
        fileItem.title = "Файл"
        menu.addItem(fileItem)
        let fileMenu = NSMenu(title: "Файл")
        fileMenu.addItem(NSMenuItem(title: "Добавить видео…", action: #selector(openVideo), keyEquivalent: "o"))
        fileMenu.addItem(NSMenuItem(title: "Обновить редактор", action: #selector(reloadEditor), keyEquivalent: "r"))
        fileMenu.addItem(.separator())
        fileMenu.addItem(NSMenuItem(title: "Открыть защищённое хранилище", action: #selector(openConfiguredStorage), keyEquivalent: ""))
        fileMenu.addItem(NSMenuItem(title: "Открыть папку видео", action: #selector(openStorageVolume), keyEquivalent: ""))
        fileItem.submenu = fileMenu

        let editItem = NSMenuItem()
        editItem.title = "Правка"
        menu.addItem(editItem)
        let editMenu = NSMenu(title: "Правка")
        editMenu.addItem(NSMenuItem(title: "Отменить изменение", action: #selector(undoEditor), keyEquivalent: "z"))
        let redo = NSMenuItem(title: "Повторить изменение", action: #selector(redoEditor), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(redo)
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(title: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(title: "Выбрать всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = editMenu
        return menu
    }

    @objc private func showAbout() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "Clip Studio", .version: "Локальное приложение для macOS"])
    }

    @objc private func openVideo() {
        guard let webView else { return }
        let script = "document.querySelector('input[type=file]')?.click()"
        webView.evaluateJavaScript(script)
    }

    @objc private func reloadEditor() { webView?.reload() }
    @objc private func undoEditor() { editorHistory("undo") }
    @objc private func redoEditor() { editorHistory("redo") }
    private func editorHistory(_ command: String) {
        webView?.evaluateJavaScript("""
            if (document.activeElement?.matches('input,textarea,select,[contenteditable=true]')) {
                document.execCommand('\(command)');
            } else {
                window.dispatchEvent(new Event('clipstudio-\(command)'));
            }
            """)
    }

    private func showStatus(_ title: String, detail: String) {
        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor(calibratedWhite: 0.075, alpha: 1).cgColor
        let heading = NSTextField(labelWithString: title)
        heading.font = .systemFont(ofSize: 20, weight: .semibold)
        heading.textColor = .white
        let message = NSTextField(wrappingLabelWithString: detail)
        message.font = .systemFont(ofSize: 13)
        message.textColor = NSColor(calibratedWhite: 0.78, alpha: 1)
        let stack = NSStackView(views: [heading, message])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: container.widthAnchor, multiplier: 0.7),
        ])
        window.contentView = container
    }

    private func showStorageGate() {
        waitingForStorage = true
        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor(calibratedWhite: 0.075, alpha: 1).cgColor

        let heading = NSTextField(labelWithString: "Откройте защищённое хранилище")
        heading.font = .systemFont(ofSize: 20, weight: .semibold)
        heading.textColor = .white
        let detail = NSTextField(wrappingLabelWithString: storageGateMessage())
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = NSColor(calibratedWhite: 0.78, alpha: 1)
        detail.alignment = .center
        storageDetailLabel = detail

        let openButton = NSButton(title: "Открыть образ в Finder", target: self, action: #selector(openConfiguredStorage))
        openButton.bezelStyle = .rounded
        openButton.isEnabled = !storageImagePath.isEmpty
        let retryButton = NSButton(title: "Проверить снова", target: self, action: #selector(checkStorageAndStart))
        retryButton.bezelStyle = .rounded
        let buttons = NSStackView(views: [openButton, retryButton])
        buttons.orientation = .horizontal
        buttons.alignment = .centerY
        buttons.spacing = 12

        let stack = NSStackView(views: [heading, detail, buttons])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: container.widthAnchor, multiplier: 0.72),
            detail.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor),
        ])
        window.contentView = container
        storageWaitTimer?.invalidate()
        storageWaitTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.checkStorageAndStart()
        }
    }

    private func storageGateMessage() -> String {
        let volume = storageVolumePath.isEmpty ? "/Volumes/Clip Vault" : storageVolumePath
        return "Подключите USB-накопитель и откройте зашифрованный образ. Введите пароль в окне Finder — Clip Studio не запрашивает и не сохраняет его. Приложение запустит службы после подключения диска в \(volume). Перед извлечением завершите Clip Studio и используйте «Извлечь» в Finder."
    }

    private func isStorageReady() -> Bool {
        guard !storageVolumePath.isEmpty else { return true }
        let expectedPath = URL(fileURLWithPath: storageVolumePath).standardizedFileURL.path
        let mounted = FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: nil, options: [])?
            .contains { $0.standardizedFileURL.path == expectedPath } ?? false
        return mounted && FileManager.default.isWritableFile(atPath: expectedPath)
    }

    @objc private func openConfiguredStorage() {
        if isStorageReady() {
            openStorageVolume()
            return
        }
        guard !storageImagePath.isEmpty else {
            storageDetailLabel?.stringValue = "Не задан путь к зашифрованному образу. Подключите диск «Clip Vault» и нажмите «Проверить снова»."
            return
        }
        let imageURL = URL(fileURLWithPath: storageImagePath).standardizedFileURL
        guard FileManager.default.fileExists(atPath: imageURL.path) else {
            storageDetailLabel?.stringValue = "Образ не найден по пути \(imageURL.path). Подключите диск с образом и нажмите «Проверить снова»."
            return
        }
        if !NSWorkspace.shared.open(imageURL) {
            storageDetailLabel?.stringValue = "Не удалось открыть образ в Finder. Откройте его вручную по пути \(imageURL.path)."
        } else {
            storageDetailLabel?.stringValue = "Finder открыл зашифрованный образ. Введите пароль только в Finder; приложение ждёт подключения диска."
        }
    }

    @objc private func openStorageVolume() {
        guard !storageVolumePath.isEmpty, isStorageMounted() else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: storageVolumePath, isDirectory: true))
    }

    private func isStorageMounted() -> Bool {
        guard !storageVolumePath.isEmpty else { return false }
        let expectedPath = URL(fileURLWithPath: storageVolumePath).standardizedFileURL.path
        return FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: nil, options: [])?
            .contains { $0.standardizedFileURL.path == expectedPath } ?? false
    }

    @objc private func checkStorageAndStart() {
        guard waitingForStorage else { return }
        guard isStorageReady() else {
            storageDetailLabel?.stringValue = storageGateMessage() + " Диск ещё не смонтирован или доступен только для чтения."
            return
        }
        waitingForStorage = false
        storageWaitTimer?.invalidate()
        storageWaitTimer = nil
        storageDetailLabel?.stringValue = "Хранилище подключено. Запускаем локальные службы…"
        startRuntime()
    }

    private func startRuntime() {
        guard let projectRoot = Bundle.main.object(forInfoDictionaryKey: "ClipStudioProjectRoot") as? String,
              let python = Bundle.main.object(forInfoDictionaryKey: "ClipStudioPython") as? String,
              let helper = Bundle.main.url(forResource: "local_desktop", withExtension: "py") else {
            showStartupError("В приложении не хватает файлов запуска. Пересоберите его командой scripts/build-desktop.sh.")
            return
        }
        do {
            startupDeadline = Date().addingTimeInterval(45)
            let bootstrap = Process()
            bootstrap.executableURL = URL(fileURLWithPath: python)
            bootstrap.arguments = [helper.path, "--project-root", projectRoot, "--init-config"]
            bootstrap.standardInput = FileHandle.nullDevice
            bootstrap.standardOutput = FileHandle.nullDevice
            bootstrap.standardError = FileHandle.nullDevice
            try bootstrap.run()
            bootstrap.waitUntilExit()
            guard bootstrap.terminationStatus == 0 else {
                showStartupError("Не удалось подготовить локальные настройки. Проверьте права на папку ~/Library/Application Support/Clip Studio.")
                return
            }
            let configURL = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Clip Studio/config.json")
            let configData = try Data(contentsOf: configURL)
            let config = try JSONSerialization.jsonObject(with: configData) as? [String: String]
            storageVolumePath = config?["CLIP_STORAGE_VOLUME"] ?? ""
            storageImagePath = config?["CLIP_STORAGE_IMAGE"] ?? ""
            username = config?["CLIP_USERNAME"] ?? ""
            password = config?["CLIP_PASSWORD"] ?? ""
            guard !username.isEmpty, !password.isEmpty else {
                showStartupError("Не найдены настройки локального входа. Проверьте config.json в папке Clip Studio.")
                return
            }
            if !isStorageReady() {
                showStorageGate()
                return
            }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [helper.path, "--project-root", projectRoot]
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            process.terminationHandler = { [weak self] process in
                DispatchQueue.main.async {
                    guard let self, !self.closing else { return }
                    self.showStartupError("Локальные службы завершились с кодом \(process.terminationStatus). Проверьте журналы в папке Clip Studio.")
                }
            }
            try process.run()
            runtime = process
            waitForHealth(projectRoot: projectRoot)
        } catch {
            showStartupError("Не удалось запустить локальные службы: \(error.localizedDescription)")
        }
    }

    private func waitForHealth(projectRoot: String) {
        let endpoint = URL(string: "http://127.0.0.1:8765/api/health")!
        var request = URLRequest(url: endpoint)
        let token = Data("\(username):\(password)".utf8).base64EncodedString()
        request.setValue("Basic \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self else { return }
            if let response = response as? HTTPURLResponse, response.statusCode == 200,
               let data, (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["ok"] as? Bool == true {
                DispatchQueue.main.async {
                    self.window.contentView = self.webView
                    self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:8765/")!))
                }
                return
            }
            if self.runtime?.isRunning == false {
                DispatchQueue.main.async { self.showStartupError("Локальные службы завершились. Проверьте журналы в ~/Library/Application Support/Clip Studio/logs.") }
                return
            }
            if Date() >= self.startupDeadline {
                DispatchQueue.main.async {
                    self.runtime?.terminate()
                    self.showStartupError("Службы не запустились за 45 секунд. Проверьте журналы и повторите запуск.")
                }
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.waitForHealth(projectRoot: projectRoot) }
        }.resume()
    }

    private func showStartupError(_ message: String) {
        guard !startupFailureShowing else { return }
        startupFailureShowing = true
        showStatus("Не удалось запустить Clip Studio", detail: message)
        let alert = NSAlert()
        alert.messageText = "Не удалось запустить Clip Studio"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Открыть журналы")
        alert.addButton(withTitle: "Повторить запуск")
        alert.addButton(withTitle: "Закрыть")
        let result = alert.runModal()
        startupFailureShowing = false
        if result == .alertFirstButtonReturn {
            let logs = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Clip Studio/logs")
            NSWorkspace.shared.open(logs)
        } else if result == .alertSecondButtonReturn {
            let previous = runtime
            if let previous, previous.isRunning { previous.terminate() }
            DispatchQueue.global(qos: .utility).async {
                previous?.waitUntilExit()
                DispatchQueue.main.async {
                    self.runtime = nil
                    self.startRuntime()
                }
            }
        } else {
            NSApp.terminate(nil)
        }
    }

    private func isLocalAppURL(_ url: URL) -> Bool {
        url.host == "127.0.0.1" && url.port == 8765
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isLocalAppURL(url) || url.scheme == "about" {
            decisionHandler(.allow)
        } else if ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        if isLocalAppURL(url) {
            webView.load(navigationAction.request)
            return nil
        }
        if ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let url = navigationResponse.response.url, isLocalAppURL(url), url.path.hasPrefix("/api/exports/") {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodHTTPBasic,
              challenge.protectionSpace.host == "127.0.0.1",
              challenge.protectionSpace.port == 8765,
              challenge.previousFailureCount == 0 else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(user: username, password: password, persistence: .forSession))
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.prompt = "Choose Video"
        panel.begin { response in completionHandler(response == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.canCreateDirectories = true
        panel.begin { completionHandler($0 == .OK ? panel.url : nil) }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.messageText = "Не удалось скачать файл"
            alert.informativeText = error.localizedDescription
            alert.alertStyle = .warning
            alert.addButton(withTitle: "Закрыть")
            alert.runModal()
        }
    }
}

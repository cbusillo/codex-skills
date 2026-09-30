// Read-only exact-list lookup. Never requests access or reads reminder contents.
import Foundation
import EventKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 2 else { fail("Expected an exact list name") }
guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess else {
    fail("Reminders access is unavailable. Use the visible manual workflow. If the invoking terminal already appears in System Settings > Privacy & Security > Reminders, the owner can enable its access there; this helper does not initiate a first-time access request.")
}
let store = EKEventStore()
let target = CommandLine.arguments[1]
let titles = store.calendars(for: .reminder).filter { $0.title == target }.map { $0.title }
do {
    let data = try JSONSerialization.data(withJSONObject: titles)
    print(String(decoding: data, as: UTF8.self))
} catch {
    fail("Cannot encode target-list lookup")
}

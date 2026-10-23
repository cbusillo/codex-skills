// EventKit supplies scheduling; the Shortcuts "Add New Reminder" action supplies the visible URL attachment.
// Reads a JSON request line on stdin, prints the exact list's items as a lookup line,
// then reads reminder_save.py's target decision line. Never requests permissions or opens the terminal URL.
import EventKit
import Foundation
import SQLite3

struct Request: Decodable {
    let kind: String?
    let `repeat`: String?
    let weekday: Int?
    let list: String
    let title: String
    let notes: String
    let url: String
    let marker: String
    let due: String
    let timezone: String
    let repeat_days: Int?
    let update_id: String?
    let delete_id: String?
    let preserve_existing: Bool?
    let shortcut: String?
    let nonce: String?
}

// reminder_save.py decides the target from the lookup; this writer only rechecks it.
struct Decision: Decodable {
    let action: String
    let id: String?
}

// Compare preserved fields after both EventKit saving and the native URL edit.
struct PreservedFields: Equatable {
    let list: String
    let title: String
    let due: DateComponents?
    let start: DateComponents?
    let timezone: TimeZone?
    let priority: Int
    let completed: Bool
    let completionDate: Date?
    let alarms: [EKAlarm]
    let recurrence: [EKRecurrenceRule]

    init(_ reminder: EKReminder) {
        list = reminder.calendar.calendarIdentifier
        title = reminder.title
        due = reminder.dueDateComponents
        start = reminder.startDateComponents
        timezone = reminder.timeZone
        priority = reminder.priority
        completed = reminder.isCompleted
        completionDate = reminder.completionDate
        alarms = (reminder.alarms ?? []).map { $0.copy() as! EKAlarm }
        recurrence = (reminder.recurrenceRules ?? []).map { $0.copy() as! EKRecurrenceRule }
    }
}

var itemID: String?
var created = false
func finish(_ status: String, _ message: String, extra: [String: Any] = [:]) -> Never {
    var receipt = extra
    receipt["status"] = status
    receipt["message"] = message
    receipt["created"] = created
    if let itemID { receipt["reminder_id"] = itemID }
    if let data = try? JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys]) {
        print(String(decoding: data, as: UTF8.self))
    }
    exit(status == "saved" || status == "deleted" ? 0 : 1)
}

func shortcutInstalled(_ name: String) -> Bool {
    let list = Process()
    let output = Pipe()
    list.executableURL = URL(fileURLWithPath: "/usr/bin/shortcuts")
    list.arguments = ["list"]
    list.standardOutput = output
    list.standardError = FileHandle.nullDevice
    guard (try? list.run()) != nil else { return false }
    let names = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    list.waitUntilExit()
    return list.terminationStatus == 0 && names.components(separatedBy: .newlines).contains(name)
}

// The shortcut's only job is Add New Reminder with this title, notes and URL; EventKit does the rest.
func runShortcut(_ name: String, title: String, notes: String, url: String) -> Bool {
    let input = FileManager.default.temporaryDirectory.appendingPathComponent("reminder-\(UUID().uuidString).json")
    defer { try? FileManager.default.removeItem(at: input) }
    guard let data = try? JSONSerialization.data(withJSONObject: ["title": title, "notes": notes, "url": url]),
          (try? data.write(to: input, options: .withoutOverwriting)) != nil else { return false }
    let run = Process()
    run.executableURL = URL(fileURLWithPath: "/usr/bin/shortcuts")
    run.arguments = ["run", name, "--input-path", input.path]
    run.standardOutput = FileHandle.nullDevice
    run.standardError = FileHandle.nullDevice
    guard (try? run.run()) != nil else { return false }
    // Stop a stalled shortcut well inside reminder_save.py's 90-second watchdog.
    let deadline = Date().addingTimeInterval(60)
    while run.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.2) }
    if run.isRunning {
        run.terminate()
        return false
    }
    return run.terminationStatus == 0
}

// EventKit cannot read the visible attachment, so read it back from the Reminders store, read-only.
func visibleURLs(_ itemID: String) -> Set<String> {
    let stores = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Group Containers/group.com.apple.reminders/Container_v1/Stores")
    let files = (try? FileManager.default.contentsOfDirectory(atPath: stores.path)) ?? []
    var urls: Set<String> = []
    for file in files where file.hasPrefix("Data-") && file.hasSuffix(".sqlite") {
        var db: OpaquePointer?
        defer { sqlite3_close(db) }
        guard sqlite3_open_v2(stores.appendingPathComponent(file).path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else { continue }
        var statement: OpaquePointer?
        defer { sqlite3_finalize(statement) }
        let query = """
            SELECT o.ZURL FROM ZREMCDOBJECT o JOIN ZREMCDREMINDER r ON o.ZREMINDER2 = r.Z_PK
            WHERE r.ZDACALENDARITEMUNIQUEIDENTIFIER = ? AND r.ZMARKEDFORDELETION = 0 AND o.ZURL IS NOT NULL
            """
        guard sqlite3_prepare_v2(db, query, -1, &statement, nil) == SQLITE_OK else { continue }
        sqlite3_bind_text(statement, 1, itemID, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
        while sqlite3_step(statement) == SQLITE_ROW {
            if let value = sqlite3_column_text(statement, 0) { urls.insert(String(cString: value)) }
        }
    }
    return urls
}

func hasVisibleURL(_ itemID: String, _ url: String) -> Bool {
    for _ in 0..<20 {
        if visibleURLs(itemID).contains(url) { return true }
        Thread.sleep(forTimeInterval: 0.5)
    }
    return false
}

guard #available(macOS 14.0, *) else { finish("manual_required", "Native saving requires macOS 14+") }
do {
    let request = try JSONDecoder().decode(Request.self, from: Data((readLine() ?? "").utf8))
    let plain = request.kind == "plain"
    let hasURL = !request.url.isEmpty
    let preserveExisting = request.preserve_existing == true
    guard !preserveExisting || (request.update_id != nil && request.delete_id == nil) else {
        finish("refused", "Preservation requires an explicit update ID; no mutation")
    }
    guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess else {
        finish("manual_required", "Existing Reminders access is required; no permission was requested")
    }
    let store = EKEventStore()
    let lists = store.calendars(for: .reminder).filter { $0.title == request.list }
    guard lists.count == 1, lists[0].allowsContentModifications else {
        finish("refused", "Expected one exact writable list; no default-list fallback")
    }
    let list = lists[0]
    let signal = DispatchSemaphore(value: 0)
    var reminders: [EKReminder]?
    store.fetchReminders(matching: store.predicateForReminders(in: [list])) {
        reminders = $0; signal.signal()
    }
    guard signal.wait(timeout: .now() + 15) == .success, let reminders else {
        finish("refused", "Exact-list lookup did not complete; no mutation")
    }
    func sameTarget(_ reminder: EKReminder) -> Bool {
        if plain { return reminder.title == request.title }
        return reminder.url?.absoluteString == request.url ||
        (reminder.notes ?? "").components(separatedBy: .newlines).contains(request.marker)
    }
    let lookup: [[String: Any]] = reminders.map {
        ["id": $0.calendarItemIdentifier, "completed": $0.isCompleted,
         "same_target": sameTarget($0), "title": $0.title ?? ""]
    }
    if let data = try? JSONSerialization.data(withJSONObject: ["status": "lookup", "reminders": lookup]) {
        print(String(decoding: data, as: UTF8.self))
    }
    fflush(stdout)
    guard let line = readLine(),
          let decision = try? JSONDecoder().decode(Decision.self, from: Data(line.utf8)) else {
        finish("refused", "No target decision; no mutation")
    }
    // Recheck the decision: only the requested incomplete item is ever changed.
    let target = reminders.first {
        !$0.isCompleted && sameTarget($0) && $0.calendarItemIdentifier == decision.id
    }
    if decision.action == "delete" {
        guard let reminder = target, let deleteID = request.delete_id, decision.id == deleteID else {
            finish("refused", "Delete ID must match the incomplete reminder in the exact list")
        }
        itemID = deleteID
        try store.remove(reminder, commit: true)
        let fresh = EKEventStore()
        guard fresh.calendarItem(withIdentifier: deleteID) == nil else {
            finish("uncertain", "Deletion absence readback failed; do not retry")
        }
        finish("deleted", "Owned reminder removed with absence readback")
    }
    var reminder: EKReminder
    if decision.action == "update", let existing = target, request.update_id == decision.id {
        itemID = existing.calendarItemIdentifier
        reminder = existing
    } else if decision.action == "create", request.update_id == nil, request.delete_id == nil {
        reminder = EKReminder(eventStore: store)
    } else {
        finish("refused", "Target decision does not match the request; no mutation")
    }
    guard !preserveExisting || reminder.title == request.title else {
        finish("refused", "Repair title must match the existing reminder; no mutation")
    }
    // EventKit URL absence alone cannot prove removal of the native attachment.
    if plain && !hasURL && reminder.url != nil {
        finish("manual_required", "Remove the existing visible link through the manual workflow; no mutation")
    }
    let preserved = preserveExisting ? PreservedFields(reminder) : nil
    if hasURL, let existingID = itemID {
        guard visibleURLs(existingID).contains(request.url) else {
            finish("refused", "This reminder has no matching visible link and Shortcuts cannot add one to an existing reminder; delete it with --delete-id and create it again. No mutation")
        }
    } else if hasURL {
        guard request.nonce != nil else { finish("refused", "A linked reminder needs its request nonce; no mutation") }
        guard let shortcut = request.shortcut, shortcutInstalled(shortcut) else {
            finish("manual_required", "Install the reminder link shortcut once with reminder_shortcut.py; no mutation")
        }
    }
    let url = hasURL ? URL(string: request.url) : nil
    guard let timezone = TimeZone(identifier: request.timezone),
          (plain ? (!hasURL || (["https", "http"].contains(url?.scheme ?? "") && url?.host != nil))
                 : url?.scheme == "iterm2"),
          (!plain || !hasURL || url?.absoluteString == request.url),
          !request.title.isEmpty, !request.list.isEmpty else {
        finish("refused", "Invalid reminder fields")
    }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.calendar = Calendar(identifier: .gregorian)
    formatter.timeZone = timezone
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm"
    formatter.isLenient = false
    guard let due = formatter.date(from: request.due), formatter.string(from: due) == request.due,
          request.repeat_days == nil || request.repeat_days! > 0 else {
        finish("refused", "Invalid due date or recurrence")
    }
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = timezone
    var recurrence: [EKRecurrenceRule] = []
    if plain {
        guard request.repeat_days == nil else { finish("refused", "Plain reminders use standard repeats") }
        switch request.repeat {
        case nil: break
        case "daily": recurrence = [EKRecurrenceRule(recurrenceWith: .daily, interval: 1, end: nil)]
        case "monthly": recurrence = [EKRecurrenceRule(recurrenceWith: .monthly, interval: 1, end: nil)]
        case "weekly", "weekdays":
            let days: [EKRecurrenceDayOfWeek]
            if request.repeat == "weekdays" {
                guard (2...6).contains(calendar.component(.weekday, from: due)) else {
                    finish("refused", "First due date must be a weekday")
                }
                days = [2, 3, 4, 5, 6].map { EKRecurrenceDayOfWeek(EKWeekday(rawValue: $0)!) }
            } else {
                guard let weekday = request.weekday, (0...6).contains(weekday),
                      calendar.component(.weekday, from: due) == ((weekday + 1) % 7) + 1 else {
                    finish("refused", "Weekly day must match the first due date")
                }
                days = [EKRecurrenceDayOfWeek(EKWeekday(rawValue: ((weekday + 1) % 7) + 1)!)]
            }
            recurrence = [EKRecurrenceRule(recurrenceWith: .weekly, interval: 1,
                daysOfTheWeek: days, daysOfTheMonth: nil, monthsOfTheYear: nil,
                weeksOfTheYear: nil, daysOfTheYear: nil, setPositions: nil, end: nil)]
        default: finish("refused", "Unsupported recurrence")
        }
        guard request.weekday == nil || request.repeat == "weekly" else {
            finish("refused", "Weekday requires weekly recurrence")
        }
    } else {
        recurrence = request.repeat_days.map {
            [EKRecurrenceRule(recurrenceWith: .daily, interval: $0, end: nil)]
        } ?? []
    }
    // Compare scheduling meaning, allowing EventKit to fill implicit calendar defaults.
    func recurrenceMatches(_ back: EKReminder) -> Bool {
        if !plain { return back.recurrenceRules?.first?.interval == request.repeat_days }
        let actual = back.recurrenceRules ?? []
        guard actual.count == recurrence.count else { return false }
        guard let expected = recurrence.first, let rule = actual.first else { return true }
        guard rule.frequency == expected.frequency, rule.interval == expected.interval,
              rule.recurrenceEnd == nil,
              (rule.monthsOfTheYear ?? []).isEmpty, (rule.weeksOfTheYear ?? []).isEmpty,
              (rule.daysOfTheYear ?? []).isEmpty, (rule.setPositions ?? []).isEmpty else { return false }
        let actualDays = rule.daysOfTheWeek ?? []
        let expectedDays = expected.daysOfTheWeek ?? []
        guard actualDays.allSatisfy({ $0.weekNumber == 0 }),
              actualDays.map({ $0.dayOfTheWeek.rawValue }).sorted() ==
                expectedDays.map({ $0.dayOfTheWeek.rawValue }).sorted() else { return false }
        let monthDays = rule.daysOfTheMonth ?? []
        return monthDays.isEmpty || (request.repeat == "monthly" &&
            monthDays == [NSNumber(value: calendar.component(.day, from: due))])
    }
    created = itemID == nil
    if hasURL && created, let shortcut = request.shortcut, let nonce = request.nonce {
        // A one-time token in Notes finds the shortcut's reminder; EventKit then sets the real fields.
        guard runShortcut(shortcut, title: request.title, notes: nonce, url: request.url) else {
            finish("uncertain", "The shortcut did not finish; search all lists for Notes \(nonce) before retrying", extra: ["nonce": nonce])
        }
        var made: [EKReminder] = []
        for _ in 0..<20 {
            let fetched = DispatchSemaphore(value: 0)
            EKEventStore().fetchReminders(matching: store.predicateForIncompleteReminders(
                withDueDateStarting: nil, ending: nil, calendars: nil)) {
                made = ($0 ?? []).filter { $0.notes == nonce }; fetched.signal()
            }
            guard fetched.wait(timeout: .now() + 15) == .success, made.isEmpty else { break }
            Thread.sleep(forTimeInterval: 0.5)
        }
        guard made.count == 1, let found = store.calendarItem(withIdentifier: made[0].calendarItemIdentifier) as? EKReminder else {
            finish("uncertain", "The shortcut's reminder was not found exactly once; search all lists for Notes \(nonce) before retrying", extra: ["nonce": nonce])
        }
        reminder = found
        itemID = found.calendarItemIdentifier
    }
    reminder.notes = request.notes
    reminder.url = url // Lookup identity only; this does not prove a visible attachment.
    if !preserveExisting {
        reminder.calendar = list
        reminder.title = request.title
        reminder.timeZone = timezone
        var components = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: due)
        components.calendar = calendar
        components.timeZone = timezone
        reminder.dueDateComponents = components
        reminder.alarms = [EKAlarm(absoluteDate: due)]
        if plain { reminder.startDateComponents = components }
        reminder.recurrenceRules = recurrence
    }
    try store.save(reminder, commit: true)
    itemID = reminder.calendarItemIdentifier
    if hasURL && !hasVisibleURL(reminder.calendarItemIdentifier, request.url) {
        finish("partial", "Reminder saved but its visible URL was not found in the Reminders store. Use this ID to inspect or remove only this item; do not create another", extra: ["url": request.url])
    }
    let fresh = EKEventStore()
    guard let back = fresh.calendarItem(withIdentifier: reminder.calendarItemIdentifier) as? EKReminder,
          back.calendar.calendarIdentifier == list.calendarIdentifier,
          back.title == request.title, (back.notes ?? "") == request.notes,
          (back.url?.absoluteString ?? "") == request.url,
          !back.isCompleted else { finish("partial", "Saved-field readback failed; do not recreate") }
    if let preserved {
        guard PreservedFields(back) == preserved else {
            finish("partial", "Repair preservation readback failed; inspect this same ID before retrying")
        }
    } else {
        guard back.dueDateComponents?.date == due,
          back.timeZone?.identifier == timezone.identifier,
          back.alarms?.count == 1, back.alarms?.first?.absoluteDate == due,
          (!plain || back.startDateComponents?.date == due),
          recurrenceMatches(back),
          !back.isCompleted else { finish("partial", "Saved-field readback failed; do not recreate") }
    }
    finish("saved", "Saved fields and any requested visible URL attachment verified", extra: ["saved_fields_verified": true, "visible_url_verified": hasURL, "preserved_existing": preserveExisting])
} catch {
    finish(itemID == nil ? "refused" : "partial", error.localizedDescription)
}

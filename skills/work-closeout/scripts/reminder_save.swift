// EventKit supplies scheduling; Reminders' native URL field supplies the visible attachment.
// Reads a JSON request line on stdin, prints the exact list's items as a lookup line,
// then reads reminder_save.py's target decision line. Never requests permissions or opens the terminal URL.
import AppKit
import ApplicationServices
import EventKit
import Foundation

struct Request: Decodable {
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

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return value
}
func text(_ element: AXUIElement, _ name: String) -> String {
    attribute(element, name) as? String ?? ""
}
func descendants(_ root: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    if depth > 22 { return [] }
    return [root] + (attribute(root, "AXChildren") as? [AXUIElement] ?? [])
        .flatMap { descendants($0, depth: depth + 1) }
}
func settleUI() { Thread.sleep(forTimeInterval: 0.3) }

func remindersWindow(_ app: NSRunningApplication) -> AXUIElement? {
    let root = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(root, 3)
    let windows = (attribute(root, "AXWindows") as? [AXUIElement] ?? []).filter { window in
        descendants(window).contains {
            text($0, "AXRole") == "AXOutline" && text($0, "AXDescription") == "Account Lists"
        }
    }
    // Settings and detached reminder windows are not list windows.
    return windows.count == 1 ? windows[0] : nil
}

// Never type through a global keystroke or clipboard: only the identified URL element.
func saveAttachment(_ request: Request, app: NSRunningApplication) -> Bool {
    app.activate()
    settleUI()
    guard let window = remindersWindow(app) else { return false }
    let listOutlines = descendants(window).filter {
        text($0, "AXRole") == "AXOutline" && text($0, "AXDescription") == "Account Lists"
    }
    guard listOutlines.count == 1 else { return false }
    let rows = descendants(listOutlines[0]).filter { element in
        text(element, "AXRole") == "AXRow" &&
        (attribute(element, "AXChildren") as? [AXUIElement] ?? []).contains {
            text($0, "AXDescription").hasPrefix(request.list + ", ")
        }
    }
    guard rows.count == 1 else { return false }
    AXUIElementSetAttributeValue(rows[0], "AXSelected" as CFString, kCFBooleanTrue)
    settleUI()
    guard attribute(rows[0], "AXSelected") as? Bool == true else { return false }

    func reminderGroups() -> [AXUIElement] {
        descendants(window).filter {
            text($0, "AXRole") == "AXGroup" &&
            (attribute($0, "AXChildren") as? [AXUIElement] ?? []).contains {
                text($0, "AXRole") == "AXTextField" && text($0, "AXValue") == request.title
            } &&
            descendants($0).contains { text($0, "AXDescription") == "Edit Details" }
        }
    }
    var groups: [AXUIElement] = []
    for _ in 0..<20 {
        groups = reminderGroups()
        if groups.count == 1 { break }
        settleUI()
    }
    guard groups.count == 1 else { return false }
    func detailsButton(_ group: AXUIElement) -> AXUIElement? {
        descendants(group).first { text($0, "AXRole") == "AXButton" && text($0, "AXDescription") == "Edit Details" }
    }
    guard let button = detailsButton(groups[0]) else { return false }
    // Some Reminders versions return cannotComplete even when the popover opens.
    AXUIElementPerformAction(button, "AXPress" as CFString)
    settleUI()
    let fields = descendants(window).filter {
        text($0, "AXRole") == "AXTextArea" && text($0, "AXDescription") == "URL"
    }
    guard fields.count == 1 else { return false }
    let field = fields[0]
    guard AXUIElementSetAttributeValue(field, "AXFocused" as CFString, kCFBooleanTrue) == .success,
          AXUIElementSetAttributeValue(field, "AXValue" as CFString, request.url as CFString) == .success,
          text(field, "AXValue") == request.url else { return false }
    // Commit by moving focus to another field, then closing Details. Escape discards edits.
    let titles = descendants(window).filter {
        text($0, "AXRole") == "AXTextArea" && text($0, "AXDescription") == "Title"
    }
    guard titles.count == 1,
          AXUIElementSetAttributeValue(titles[0], "AXFocused" as CFString, kCFBooleanTrue) == .success else { return false }
    AXUIElementPerformAction(button, "AXPress" as CFString)
    settleUI()
    // Attachment proof is a link in this reminder's group, never EKReminder.url readback.
    for _ in 0..<20 {
        groups = reminderGroups()
        if groups.count == 1 && descendants(groups[0]).contains(where: {
            text($0, "AXRole") == "AXLink" && text($0, "AXTitle") == request.url
        }) { return true }
        settleUI()
    }
    return false
}

guard #available(macOS 14.0, *) else { finish("manual_required", "Native saving requires macOS 14+") }
do {
    let request = try JSONDecoder().decode(Request.self, from: Data((readLine() ?? "").utf8))
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
        reminder.url?.absoluteString == request.url ||
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
    // Recheck the decision: only the requested incomplete session item is ever changed.
    let target = reminders.first {
        !$0.isCompleted && sameTarget($0) && $0.calendarItemIdentifier == decision.id
    }
    if decision.action == "delete" {
        guard let reminder = target, let deleteID = request.delete_id, decision.id == deleteID else {
            finish("refused", "Delete ID must match the incomplete session in the exact list")
        }
        itemID = deleteID
        try store.remove(reminder, commit: true)
        let fresh = EKEventStore()
        guard fresh.calendarItem(withIdentifier: deleteID) == nil else {
            finish("uncertain", "Deletion absence readback failed; do not retry")
        }
        finish("deleted", "Owned reminder removed with absence readback")
    }
    let reminder: EKReminder
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
    let preserved = preserveExisting ? PreservedFields(reminder) : nil
    guard AXIsProcessTrusted() else {
        finish("manual_required", "Existing Accessibility access is required to save the visible URL; no mutation")
    }
    guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.reminders").first else {
        finish("manual_required", "Open Reminders before saving; no mutation")
    }
    guard remindersWindow(app) != nil else {
        finish("manual_required", "Open one Reminders list window with its sidebar visible; close extra list windows before saving. No mutation")
    }
    guard let timezone = TimeZone(identifier: request.timezone),
          let url = URL(string: request.url), url.scheme == "iterm2",
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
    created = itemID == nil
    reminder.notes = request.notes
    reminder.url = url // Lookup identity only; this does not prove a visible attachment.
    if !preserveExisting {
        reminder.calendar = list
        reminder.title = request.title
        reminder.timeZone = timezone
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        var components = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: due)
        components.calendar = calendar
        components.timeZone = timezone
        reminder.dueDateComponents = components
        reminder.alarms = [EKAlarm(absoluteDate: due)]
        reminder.recurrenceRules = request.repeat_days.map {
            [EKRecurrenceRule(recurrenceWith: .daily, interval: $0, end: nil)]
        } ?? []
    }
    try store.save(reminder, commit: true)
    itemID = reminder.calendarItemIdentifier
    guard saveAttachment(request, app: app) else {
        finish("partial", "Reminder saved but visible URL not verified. Use this ID to finish or remove only this item; do not create another", extra: ["url": request.url])
    }
    let fresh = EKEventStore()
    guard let back = fresh.calendarItem(withIdentifier: reminder.calendarItemIdentifier) as? EKReminder,
          back.calendar.calendarIdentifier == list.calendarIdentifier,
          back.title == request.title, back.notes == request.notes,
          back.url?.absoluteString == request.url,
          !back.isCompleted else { finish("partial", "Saved-field readback failed; do not recreate") }
    if let preserved {
        guard PreservedFields(back) == preserved else {
            finish("partial", "Repair preservation readback failed; inspect this same ID before retrying")
        }
    } else {
        guard back.dueDateComponents?.date == due,
          back.timeZone?.identifier == timezone.identifier,
          back.alarms?.count == 1, back.alarms?.first?.absoluteDate == due,
          (back.recurrenceRules?.first?.interval) == request.repeat_days,
          !back.isCompleted else { finish("partial", "Saved-field readback failed; do not recreate") }
    }
    finish("saved", "Native visible URL attachment and saved fields verified", extra: ["visible_url_verified": true, "preserved_existing": preserveExisting])
} catch {
    finish(itemID == nil ? "refused" : "partial", error.localizedDescription)
}

// swift-tools-version: 5.9
import PackageDescription

// Give CodeQL's Swift extractor a build target for the existing native helper.
let package = Package(
    name: "CodexSkillsNativeHelpers",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "reminder-list", targets: ["ReminderList"]),
        .executable(name: "reminder-save", targets: ["ReminderSave"])
    ],
    targets: [
        .executableTarget(
            name: "ReminderList",
            path: "skills/work-closeout/scripts",
            sources: ["reminder_list.swift"]
        ),
        .executableTarget(
            name: "ReminderSave",
            path: "skills/work-closeout/scripts",
            sources: ["reminder_save.swift"]
        )
    ]
)

// ad-multiout — build/refresh the stacked aggregate ("Multi-Output") device
// used during capture.
//
// WHY THIS EXISTS:
// Capture routes system audio into a loopback device (BlackHole) while the
// user's real output stays in the chain. macOS has no CLI for creating a
// Multi-Output Device, so this does it via the CoreAudio HAL.
//
// It REPLACES the previous closed binary, which had its product name, its
// aggregate UID and its own source path baked in as C string literals with no
// room to grow. Everything is now a parameter, so renaming the product never
// requires touching a binary again.
//
// Usage:
//   ad-multiout <audible-device-name>     build/refresh the aggregate
//   ad-multiout --destroy                 remove it again (used by the uninstaller)
//
// Environment overrides (all optional):
//   AD_MULTIOUT_NAME     display name of the aggregate   (default "Audio Downloader")
//   AD_MULTIOUT_UID      stable UID of the aggregate     (default "com.audiodownloader.aggregate.01")
//   AD_LOOPBACK_DEVICE   loopback device to add          (default "BlackHole 2ch")
//
// Exit codes: 0 ok; 1 device lookup failed; 2 create failed; 64 usage error.

import Foundation
import CoreAudio

let SYSTEM = AudioObjectID(kAudioObjectSystemObject)

func err(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func env(_ key: String, _ fallback: String) -> String {
    guard let v = ProcessInfo.processInfo.environment[key], !v.isEmpty else { return fallback }
    return v
}

// MARK: - HAL helpers

func allDeviceIDs() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(0)
    guard AudioObjectGetPropertyDataSize(SYSTEM, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(SYSTEM, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func stringProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: CFString? = nil
    let status = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    guard status == noErr, let cf = value else { return nil }
    return cf as String
}

/// True when the device exposes at least one output channel. Input-only
/// devices can carry the same name as an output (and aggregates list both),
/// so every lookup here is filtered on this.
func hasOutputChannels(_ id: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioObjectPropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(0)
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return false }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return false }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    for buffer in list where buffer.mNumberChannels > 0 { return true }
    return false
}

func deviceName(_ id: AudioObjectID) -> String? {
    stringProperty(id, kAudioDevicePropertyDeviceNameCFString) ?? stringProperty(id, kAudioObjectPropertyName)
}

func deviceUID(_ id: AudioObjectID) -> String? {
    stringProperty(id, kAudioDevicePropertyDeviceUID)
}

/// Resolve an output device by display name. Exact match wins; a
/// case-insensitive match is accepted as a fallback so config drift over
/// capitalisation ("blackhole 2ch") does not break capture.
func findOutputUID(named wanted: String) -> String? {
    var fuzzy: String? = nil
    for id in allDeviceIDs() {
        guard hasOutputChannels(id), let name = deviceName(id), let uid = deviceUID(id) else { continue }
        if name == wanted { return uid }
        if fuzzy == nil, name.compare(wanted, options: .caseInsensitive) == .orderedSame { fuzzy = uid }
    }
    return fuzzy
}

func findDeviceID(uid wanted: String) -> AudioObjectID? {
    for id in allDeviceIDs() where deviceUID(id) == wanted { return id }
    return nil
}

// MARK: - main

let args = CommandLine.arguments
let tool = URL(fileURLWithPath: args.first ?? "ad-multiout").lastPathComponent
guard args.count == 2, !args[1].isEmpty else {
    err("usage: \(tool) <audible-device-name>")
    err("       \(tool) --destroy")
    exit(64)
}

let aggregateName = env("AD_MULTIOUT_NAME", "Audio Downloader")
let aggregateUID = env("AD_MULTIOUT_UID", "com.audiodownloader.aggregate.01")
let loopbackName = env("AD_LOOPBACK_DEVICE", "BlackHole 2ch")

// Uninstall path: take the aggregate back out of the system so removing the
// app does not leave an orphan device sitting in Audio MIDI Setup forever.
if args[1] == "--destroy" {
    guard let victim = findDeviceID(uid: aggregateUID) else {
        print("no aggregate with uid \(aggregateUID) — nothing to remove")
        exit(0)
    }
    let st = AudioHardwareDestroyAggregateDevice(victim)
    if st != noErr {
        err("destroy failed status=\(st)")
        exit(2)
    }
    print("removed \(aggregateName) (\(aggregateUID))")
    exit(0)
}

let audibleName = args[1]

guard let loopbackUID = findOutputUID(named: loopbackName) else {
    err("\(loopbackName) not found")
    exit(1)
}
guard let audibleUID = findOutputUID(named: audibleName) else {
    err("primary device '\(audibleName)' not found")
    exit(1)
}

// Tear down any previous instance of OUR aggregate so the subdevice list
// always reflects the device the user is actually listening on right now.
// Matching is by UID, so a user-made Multi-Output is never touched.
if let stale = findDeviceID(uid: aggregateUID) {
    let status = AudioHardwareDestroyAggregateDevice(stale)
    if status != noErr { err("WARN destroy status=\(status)") }
    // The HAL publishes device teardown asynchronously; creating the
    // replacement in the same tick can otherwise collide with the old UID.
    Thread.sleep(forTimeInterval: 0.15)
}

let description: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: aggregateName,
    kAudioAggregateDeviceUIDKey as String: aggregateUID,
    kAudioAggregateDeviceIsStackedKey as String: 1,   // 1 = Multi-Output, not an aggregate input
    kAudioAggregateDeviceIsPrivateKey as String: 0,   // visible in Audio MIDI Setup
    kAudioAggregateDeviceMasterSubDeviceKey as String: audibleUID,
    kAudioAggregateDeviceSubDeviceListKey as String: [
        [kAudioSubDeviceUIDKey as String: audibleUID],
        // Drift compensation on the loopback only: the audible device is the
        // clock master, so compensating it against itself would be a no-op
        // that some drivers report as an error.
        [kAudioSubDeviceUIDKey as String: loopbackUID,
         kAudioSubDeviceDriftCompensationKey as String: 1],
    ],
]

var aggregateID = AudioObjectID(0)
let status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID)
guard status == noErr, aggregateID != 0 else {
    err("create failed status=\(status)")
    exit(2)
}

print("\(aggregateName): \(audibleName) + \(loopbackName) (deviceID=\(aggregateID))")
exit(0)

// avrec — record a named CoreAudio input device to a .caf via AVAudioEngine.
// usage: avrec <device-name> <output.caf> [seconds]
// SIGTERM stops cleanly so the file flushes.
import Foundation
import AVFoundation
import CoreAudio
import AudioToolbox

func err(_ s: String) {
    FileHandle.standardError.write(s.data(using: .utf8)!)
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    err("usage: avrec <device-name> <output.caf> [seconds]\n")
    exit(2)
}
let wantName = args[1]
let outPath  = args[2]
let seconds: Double? = args.count >= 4 ? Double(args[3]) : nil

// ---- find the device id by name -------------------------------------------
func allDeviceIDs() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size) == noErr else { return [] }
    let n = Int(size) / MemoryLayout<AudioDeviceID>.size
    var ids = [AudioDeviceID](repeating: 0, count: n)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func deviceName(_ id: AudioDeviceID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let st = withUnsafeMutablePointer(to: &name) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    return st == noErr ? (name as String) : ""
}

var devID: AudioDeviceID? = nil
for id in allDeviceIDs() where deviceName(id) == wantName { devID = id; break }
if devID == nil {
    for id in allDeviceIDs() where deviceName(id).hasPrefix(wantName) { devID = id; break }
}
guard let deviceID = devID else {
    err("device '\(wantName)' not found\n")
    exit(3)
}

// ---- point the engine's input at it ---------------------------------------
let engine = AVAudioEngine()
let input  = engine.inputNode          // realises the HAL audio unit
var dev = deviceID
let setStatus = AudioUnitSetProperty(input.audioUnit!,
                                     kAudioOutputUnitProperty_CurrentDevice,
                                     kAudioUnitScope_Global, 0,
                                     &dev, UInt32(MemoryLayout<AudioDeviceID>.size))
if setStatus != noErr {
    err("AudioUnitSetProperty CurrentDevice failed: \(setStatus)\n")
    exit(4)
}

let fmt = input.inputFormat(forBus: 0)
let file: AVAudioFile
do {
    file = try AVAudioFile(forWriting: URL(fileURLWithPath: outPath),
                           settings: fmt.settings,
                           commonFormat: .pcmFormatFloat32,
                           interleaved: false)
} catch {
    err("file open: \(error)\n")
    exit(5)
}

let lock = NSLock()
var stopping = false

input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { buf, _ in
    lock.lock(); defer { lock.unlock() }
    if stopping { return }
    do { try file.write(from: buf) } catch { err("write: \(error)\n") }
}

let t0 = Date()
do {
    try engine.start()
} catch {
    err("engine start: \(error)\n")
    exit(6)
}
err("recording -> \(outPath)\n")

func finish() -> Never {
    lock.lock()
    stopping = true
    lock.unlock()
    input.removeTap(onBus: 0)
    engine.stop()
    err("done wall=\(String(format: "%.3f", Date().timeIntervalSince(t0)))\n")
    exit(0)
}

let sigsrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigsrc.setEventHandler { finish() }
sigsrc.resume()
signal(SIGTERM, SIG_IGN)

let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigint.setEventHandler { finish() }
sigint.resume()
signal(SIGINT, SIG_IGN)

if let s = seconds, s > 0 {
    RunLoop.main.run(until: Date().addingTimeInterval(s))
    finish()
} else {
    RunLoop.main.run()
}

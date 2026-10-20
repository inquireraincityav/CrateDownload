// ensure-blackhole-rate — pin the BlackHole 2ch virtual device to 44100 Hz.
import Foundation
import CoreAudio

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

var rateAddr = AudioObjectPropertyAddress(
    mSelector: kAudioDevicePropertyNominalSampleRate,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)

func getRate(_ id: AudioDeviceID) -> (Float64, OSStatus) {
    var r: Float64 = 0
    var size = UInt32(MemoryLayout<Float64>.size)
    let st = AudioObjectGetPropertyData(id, &rateAddr, 0, nil, &size, &r)
    return (r, st)
}

let target: Float64 = CommandLine.arguments.count > 1
    ? (Double(CommandLine.arguments[1]) ?? 44100) : 44100

var found: AudioDeviceID? = nil
for id in allDeviceIDs() where deviceName(id).hasPrefix("BlackHole 2ch") { found = id; break }
guard let dev = found else {
    print("BlackHole 2ch not found")
    exit(1)
}

let (before, st1) = getRate(dev)
print("BlackHole current rate: \(before) (status \(st1))")

if before == target {
    print("Set \(Int(target)) status: 0")
} else {
    var want = target
    let st = AudioObjectSetPropertyData(dev, &rateAddr, 0, nil,
                                        UInt32(MemoryLayout<Float64>.size), &want)
    print("Set \(Int(target)) status: \(st)")
    usleep(300_000)
}

let (after, st2) = getRate(dev)
print("BlackHole rate after: \(after) (status \(st2))")
exit(after == target ? 0 : 1)

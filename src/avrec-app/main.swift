// avrec-app — per-app audio capture via ScreenCaptureKit
// Usage: avrec-app <bundle-id> <output.caf> [seconds]
//
// Captures audio from a single running application (matched by bundle ID)
// using SCStream. Writes a CAF file in the format SCK delivers (typically
// Float32 non-interleaved stereo @ 48 kHz). The downstream encode_caf_to_mp3
// step in record-audio handles whatever sample rate / format SCK gives us.
//
// SIGTERM stops the stream cleanly and exits 0. Permission failures (Screen
// Recording TCC denied) print a clear hint and exit 3.

import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia

// MARK: - args
let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: avrec-app <bundle-id> <output.caf> [seconds]\n".data(using: .utf8)!)
    exit(64)
}
let bundleID = args[1]
let outPath = (args[2] as NSString).expandingTildeInPath
let durationSec: TimeInterval? = args.count > 3 ? TimeInterval(args[3]) : nil

func stderr(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

// MARK: - CMSampleBuffer → AVAudioPCMBuffer
extension CMSampleBuffer {
    var asPCMBuffer: AVAudioPCMBuffer? {
        try? self.withAudioBufferList { audioBufferList, _ in
            guard let absd = self.formatDescription?.audioStreamBasicDescription else { return nil }
            guard let format = AVAudioFormat(standardFormatWithSampleRate: absd.mSampleRate,
                                             channels: absd.mChannelsPerFrame) else { return nil }
            return AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: audioBufferList.unsafePointer)
        }
    }
}

// MARK: - capture engine
final class CaptureEngine: NSObject, SCStreamOutput, SCStreamDelegate {
    private var audioFile: AVAudioFile?
    private var stream: SCStream?
    private let outURL: URL
    private let queue = DispatchQueue(label: "avrec-app.audio")
    private var stopped = false

    init(outURL: URL) {
        self.outURL = outURL
        super.init()
    }

    func start(filter: SCContentFilter, config: SCStreamConfiguration) async throws {
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        self.stream = stream
        try await stream.startCapture()
    }

    func stop() async {
        guard !stopped else { return }
        stopped = true
        if let stream = stream {
            try? await stream.stopCapture()
        }
        // closing AVAudioFile is implicit on dealloc; release the ref so the
        // file's backing data is flushed and the CAF header is finalized.
        audioFile = nil
    }

    // SCStreamOutput
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let pcm = sampleBuffer.asPCMBuffer else { return }

        if audioFile == nil {
            // Lazily create the file using the format of the first buffer so the
            // CAF matches whatever sample rate / channel count SCK delivers.
            do {
                audioFile = try AVAudioFile(
                    forWriting: outURL,
                    settings: pcm.format.settings,
                    commonFormat: .pcmFormatFloat32,
                    interleaved: false
                )
            } catch {
                stderr("avrec-app: AVAudioFile open failed: \(error)")
                return
            }
        }

        do {
            try audioFile?.write(from: pcm)
        } catch {
            stderr("avrec-app: write failed: \(error)")
        }
    }

    // SCStreamDelegate
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        stderr("avrec-app: stream stopped with error: \(error)")
        Task { await self.stop(); exit(2) }
    }
}

// MARK: - SCK content selection
func findSpotifyApp() async throws -> SCRunningApplication {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let app = content.applications.first(where: { $0.bundleIdentifier == bundleID }) else {
        stderr("avrec-app: no running app with bundle id \(bundleID)")
        stderr("avrec-app: hint — make sure the app is launched and currently running")
        exit(2)
    }
    return app
}

func makeFilter(app: SCRunningApplication) async throws -> SCContentFilter {
    // SCContentFilter requires a display anchor for audio-only too.
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let display = content.displays.first else {
        stderr("avrec-app: no displays — cannot build SCContentFilter")
        exit(2)
    }
    return SCContentFilter(display: display, including: [app], exceptingWindows: [])
}

// MARK: - main
let outURL = URL(fileURLWithPath: outPath)
let engine = CaptureEngine(outURL: outURL)

// Forward SIGTERM/SIGINT to a clean shutdown
let sigSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
let intSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigSrc.setEventHandler { Task { await engine.stop(); exit(0) } }
intSrc.setEventHandler { Task { await engine.stop(); exit(0) } }
signal(SIGTERM, SIG_IGN); signal(SIGINT, SIG_IGN)
sigSrc.resume(); intSrc.resume()

Task {
    do {
        let app = try await findSpotifyApp()
        let filter = try await makeFilter(app: app)
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // Video capture is required by SCStream init even though we want audio
        // only — set the smallest possible video frame and ignore those buffers.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        try await engine.start(filter: filter, config: config)
        stderr("avrec-app: capturing \(bundleID) → \(outPath)")

        if let dur = durationSec {
            try await Task.sleep(nanoseconds: UInt64(dur * 1_000_000_000))
            await engine.stop()
            exit(0)
        }
    } catch {
        let nsErr = error as NSError
        if nsErr.domain == "com.apple.ScreenCaptureKit.SCStreamErrorDomain" || String(describing: error).contains("permission") {
            stderr("avrec-app: ScreenCaptureKit permission denied. Grant Screen Recording in System Settings → Privacy & Security → Screen Recording.")
            exit(3)
        }
        stderr("avrec-app: fatal: \(error)")
        exit(2)
    }
}

dispatchMain()

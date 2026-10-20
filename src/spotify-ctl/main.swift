// spotify-ctl — fixed-path Spotify AppleScript bridge.
//
// WHY THIS EXISTS:
// macOS keys the AppleEvents (Automation) TCC grant to the *sending binary's
// path*. record-audio used to send Spotify AppleScript via /usr/bin/osascript
// as a child of the version-pathed `claude` binary, so macOS attributed the
// grant to claude — and every Claude update lands at a new versioned path,
// dropping the grant. This tiny compiled binary lives at a STABLE path
// (~/.claude/bin/spotify-ctl). Approve its "control Spotify" prompt ONCE and
// the grant persists across every future Claude update. Same durable pattern
// as avrec-app's Screen Recording grant.
//
// Usage:
//   spotify-ctl raw '<applescript source>'   -> run arbitrary AppleScript
//   spotify-ctl ping                          -> "Spotify" if controllable
//   spotify-ctl state                         -> player state
//
// Exit codes: 0 ok; 1 AppleScript runtime error; 2 compile error;
//             64 usage error. AppleScript error number is echoed to stderr
//             (e.g. -1728 "no current track") so callers can distinguish
//             "logged out / nothing loaded" from a hard failure.

import Foundation

func err(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func runAppleScript(_ src: String) -> Int32 {
    var errorDict: NSDictionary?
    guard let script = NSAppleScript(source: src) else {
        err("spotify-ctl: failed to compile AppleScript")
        return 2
    }
    let result = script.executeAndReturnError(&errorDict)
    if let e = errorDict {
        let msg = (e[NSAppleScript.errorMessage] as? String) ?? "\(e)"
        let num = (e[NSAppleScript.errorNumber] as? Int) ?? 0
        err("spotify-ctl: AppleScript error \(num): \(msg)")
        return 1
    }
    if let out = result.stringValue {
        FileHandle.standardOutput.write(out.data(using: .utf8)!)
    }
    return 0
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    err("usage: spotify-ctl raw <applescript> | spotify-ctl ping | spotify-ctl state")
    exit(64)
}

switch args[1] {
case "raw":
    guard args.count >= 3 else { err("spotify-ctl raw: missing applescript"); exit(64) }
    exit(runAppleScript(args[2]))
case "ping":
    exit(runAppleScript("tell application \"Spotify\" to return name"))
case "state":
    exit(runAppleScript("tell application \"Spotify\" to return player state as text"))
default:
    err("spotify-ctl: unknown subcommand \(args[1])")
    exit(64)
}

// Afterthought's pi adapter: hand pi's session lifecycle to `aft hook pi <event>` and put
// the block aft printed back in front of the model (BUILD-PLAN M1.4, DESIGN 8 "Adapters in
// v1"). The install side (`aft install pi`) copies this file to
// ~/.pi/agent/extensions/afterthought/index.ts.
//
// Thin on purpose, like pi's own rtk extension: every memory decision lives in `aft`. This
// file runs one command per event, keeps the one block that command printed, and adds that
// block as a system prompt section of its own. It has no queue, no retries, no cache and no
// settings of its own.
//
// The payload is the shape src/aft/adapters/pi.py parses, and the event names are that
// module's own table: `session_start`, `session_before_compact`, `session_shutdown`.
// `aft hook` never fails and always exits 0 (BUILD-PLAN M1.1), so nothing here reads an exit
// code: what it prints is the whole conversation between the two.

import { spawn } from "node:child_process"
import type { ChildProcess } from "node:child_process"
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"

const AFT = "aft"

// The outside half of the bound in aft's own HOOK_TIMEOUT_SECONDS (src/aft/cli.py). A slow,
// hung or missing `aft` costs a pi session at most this many milliseconds, including on
// shutdown, where pi is waiting to close.
const HOOK_TIMEOUT_MS = 2_000

// pi renders each named section as <name>content</name> and replaces a section by name, so
// the block arrives once per session and a refreshed block stands in for the text it
// replaces instead of arriving a second time beside it.
const SECTION = "afterthought"

/** What `aft hook pi <event>` reads on stdin, field for field with src/aft/adapters/pi.py. */
interface HookPayload {
  session_id: string
  cwd: string
  transcript_path?: string
  hook_event_name: string
  reason?: string
}

/** The three facts about a session that aft reads, as pi's session manager exposes them. */
interface SessionInfo {
  getSessionId(): string
  getCwd(): string
  getSessionFile(): string | undefined
}

export default function (pi: ExtensionAPI) {
  // The block aft printed for this session, and the one this extension sends to the model.
  let block = ""
  let missingAftReported = false

  pi.on("session_start", async (event, ctx) => {
    block = await hook("session_start", payload("session_start", event.reason, ctx.sessionManager))
  })

  pi.on("before_agent_start", (event) => {
    // Contribute the block as its own section. The host's prompt, its sections and its
    // guidelines are left exactly as they arrived.
    if (block) {
      event.systemPromptOptions.sections[SECTION] = block
    }
  })

  pi.on("session_before_compact", async (event, ctx) => {
    // Refresh the kept block from the re-run. A compaction is dispatched to capture, not to
    // injection (BUILD-PLAN M1.1), so aft normally prints nothing here, and nothing printed
    // means there is nothing new to keep: the block in hand is the one worth carrying.
    const refreshed = await hook(
      "session_before_compact",
      payload("session_before_compact", event.reason, ctx.sessionManager),
    )
    if (refreshed) {
      block = refreshed
    }
  })

  pi.on("session_shutdown", async (event, ctx) => {
    await hook("session_shutdown", payload("session_shutdown", event.reason, ctx.sessionManager))
  })

  /**
   * Run `aft hook pi <event>` with one payload on stdin and return what it printed.
   *
   * Never throws and never rejects: an agent is not improved by a memory adapter that can
   * break it. A missing `aft` is reported once, since every event would otherwise repeat the
   * same line, and the reason is aft's own to log (~/.afterthought/logs/hooks.log).
   */
  async function hook(event: string, body: HookPayload): Promise<string> {
    const printed = await new Promise<string>((resolve) => {
      let child: ChildProcess
      try {
        child = spawn(AFT, ["hook", "pi", event], { stdio: ["pipe", "pipe", "ignore"] })
      } catch {
        resolve("")
        return
      }
      let stdout = ""
      let settled = false
      let expiry: ReturnType<typeof setTimeout>
      const finish = (text: string) => {
        if (settled) {
          return
        }
        settled = true
        clearTimeout(expiry)
        resolve(text)
      }
      // A command killed on the bound leaves only the bytes that had reached the
      // pipe, and a truncated memory block injected for the rest of a session is
      // worse than no block at all, so nothing received so far is kept.
      expiry = setTimeout(() => {
        child.kill()
        finish("")
      }, HOOK_TIMEOUT_MS)
      // Decoded as a stream rather than per chunk: a chunk boundary falls at an
      // arbitrary byte, and a character straddling one survives only when the
      // decoder holds the partial sequence until its remainder arrives.
      child.stdout?.setEncoding("utf8")
      child.stdout?.on("data", (chunk: string) => {
        stdout += chunk
      })
      child.on("error", () => {
        if (!missingAftReported) {
          missingAftReported = true
          console.warn(`[afterthought] cannot run \`${AFT}\` - is it installed and in PATH?`)
        }
        finish("")
      })
      child.on("close", () => finish(stdout))
      // A command that exits before reading its whole stdin leaves a broken pipe behind, and
      // the exit is the fact that matters, not the pipe.
      child.stdin?.on("error", () => {})
      child.stdin?.end(JSON.stringify(body))
    })
    return printed.trim()
  }
}

/** One payload, as the adapter on the other end names its fields. */
function payload(event: string, reason: string | undefined, session: SessionInfo): HookPayload {
  return {
    session_id: session.getSessionId(),
    cwd: session.getCwd(),
    // Undefined until pi first writes the session file, and JSON.stringify leaves an
    // undefined field out, which src/aft/adapters/pi.py reads as a session with no file yet.
    transcript_path: session.getSessionFile(),
    // Repeats the event the command line already names, as the captured payloads do. aft
    // reads the command line and ignores this field.
    hook_event_name: event,
    reason,
  }
}

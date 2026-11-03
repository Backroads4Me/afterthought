// Afterthought's omp (Oh My Pi) adapter: hand omp's session lifecycle to `aft hook omp
// <event>` and put the block aft printed back in front of the model. The install side
// (`aft install omp`) copies this file to ~/.omp/agent/hooks/pre/afterthought.ts.
//
// Thin on purpose, like the pi extension beside it (integrations/pi/afterthought.ts): every
// memory decision lives in `aft`. This file runs one command per event, keeps the one block
// that command printed, and puts that block in front of the model. `aft hook omp
// session_start` prints the block `aft inject` prints for the session's directory; the
// capture events print nothing. When notes in memory wait on the user, session_start prints
// a JSON object instead, `{"block", "notice"}`, and the notice is shown to the user. It has
// no queue, no retries, no cache and no settings of its own.
//
// The payload is the shape src/aft/adapters/omp.py parses, and the event names are that
// module's own table: `session_start`, `session_before_compact`, `session_compact`,
// `session_shutdown`, `before_agent_start`. `aft hook` never fails and always exits 0, so
// nothing here reads an exit code: what it prints is the whole conversation between the two.
//
// Where omp differs from pi, and why this is a file of its own rather than a copy:
//
// * pi takes system prompt sections by name, `event.systemPromptOptions.sections`. omp 18.16
//   has no such field. Its `before_agent_start` carries the turn's base prompt as
//   `systemPrompt: string[]` and reads a replacement back from the handler's result, which
//   omp applies as a per-turn override built on that base (omp's
//   session/session-tools.d.ts, `setTurnSystemPromptOverride`). So each turn returns the base
//   with afterthought's blocks appended, and each turn drops the blocks a previous turn or a
//   chained extension already appended, because an array has no names to replace by.
// * omp discovers a hook module from `<agent-dir>/hooks/pre/*.ts` and loads it as an
//   extension module, so the factory is handed omp's `ExtensionAPI` rather than the legacy
//   `HookAPI` - which is what makes the `systemPrompt` result honored here (omp's
//   hooks.md, "Discovered factories get ExtensionAPI/ExtensionContext").
// * `before_agent_start` also fires with `reason: "revive"` for a parked subagent, with an
//   empty prompt and its result discarded, so a revive asks aft for nothing.
// * pi's session events carry a `reason` saying what set them off - startup, reload, quit,
//   threshold. omp's carry none at all: `SessionStartEvent`, `SessionCompactEvent` and
//   `SessionShutdownEvent` hold their type alone, and `SessionBeforeCompactEvent` holds the
//   compaction's own preparation (omp's extensibility/shared-events.d.ts). So no payload from
//   here names a reason, and src/aft/adapters/omp.py reads one that is absent as a session
//   event with no detail.
import { spawn } from "node:child_process"
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent/extensibility/extensions"

const AFT = "aft"

// The agent name `aft hook` is called with: omp keeps its own adapter, reader and digest
// names rather than passing for pi (src/aft/adapters/omp.py).
const AGENT = "omp"

// The outside half of the bound in aft's own HOOK_TIMEOUT_SECONDS (src/aft/cli.py). A slow,
// hung or missing `aft` costs an omp session at most this many milliseconds, including on
// shutdown, where omp is waiting to close.
const HOOK_TIMEOUT_MS = 2_000

// The block's own tag in the system prompt, and the prompt recall's beside it. omp's system
// prompt is an array of text blocks with no names, so the tags are what let a later turn
// find the blocks of an earlier one and leave one copy of each in front of the model.
const SECTION = "afterthought"
const RECALL_SECTION = "afterthought-recall"

/** What `aft hook omp <event>` reads on stdin, field for field with src/aft/adapters/omp.py. */
interface HookPayload {
  session_id: string
  cwd: string
  transcript_path?: string
  hook_event_name: string
  /** The submitted prompt, for before_agent_start only. */
  prompt?: string
}

/** The three facts about a session that aft reads, as omp's session manager exposes them. */
interface SessionInfo {
  getSessionId(): string
  getCwd(): string
  getSessionFile(): string | undefined
}

export default function (pi: ExtensionAPI) {
  // The block aft printed for this session, and the one this hook sends to the model.
  let block = ""
  // The recall aft printed for the newest prompt. Each turn starts it empty, so a turn with
  // no prompt to recall for - an image alone, a session that says nothing - sends the
  // previous prompt's recall nowhere.
  let recall = ""
  let missingAftReported = false

  pi.on("session_start", async (event, ctx) => {
    try {
      const p = payload("session_start", ctx?.sessionManager, ctx?.cwd)
      if (p) {
        const start = startOutput(await hook("session_start", p))
        block = start.block
        if (start.notice && ctx?.hasUI) {
          ctx.ui.notify(start.notice, "warning")
        }
      }
    } catch {
      // Best-effort by design: memory never breaks the agent (see the header comment).
    }
  })

  pi.on("before_agent_start", async (event, ctx) => {
    // A parked subagent's revive fires this event with an empty prompt and discards the
    // result, so there is nothing to recall for and nothing to inject into.
    if (event?.reason === "revive") {
      return undefined
    }
    recall = ""
    try {
      const p = payload("before_agent_start", ctx?.sessionManager, ctx?.cwd)
      if (p && typeof event?.prompt === "string" && event.prompt) {
        p.prompt = event.prompt
        recall = await hook("before_agent_start", p)
      }
    } catch {
      // Best-effort by design: memory never breaks the agent (see the header comment).
    }
    try {
      const base = Array.isArray(event?.systemPrompt) ? event.systemPrompt : []
      const sections = [tagged(SECTION, block), tagged(RECALL_SECTION, recall)].filter(Boolean)
      if (!sections.length) {
        return undefined
      }
      // Our own blocks come off the base before they go back on, so a turn that already
      // carries them - a rebuilt prompt, or another extension's chained result - ends with
      // one copy of each rather than a second one beside the first.
      return { systemPrompt: [...base.filter((text) => !isOurs(text)), ...sections] }
    } catch {
      // Best-effort by design: memory never breaks the agent (see the header comment).
      return undefined
    }
  })

  pi.on("session_before_compact", async (event, ctx) => {
    try {
      const p = payload("session_before_compact", ctx?.sessionManager, ctx?.cwd)
      if (p) {
        await hook("session_before_compact", p)
      }
    } catch {
      // Best-effort by design: memory never breaks the agent (see the header comment).
    }
  })

  pi.on("session_compact", async (event, ctx) => {
    // Refresh the kept block after compaction.
    try {
      const p = payload("session_start", ctx?.sessionManager, ctx?.cwd)
      if (p) {
        const refreshed = startOutput(await hook("session_start", p)).block
        if (refreshed) {
          block = refreshed
        }
      }
    } catch {
      // Best-effort by design: memory never breaks the agent (see the header comment).
    }
  })

  pi.on("session_shutdown", async (event, ctx) => {
    try {
      const p = payload("session_shutdown", ctx?.sessionManager, ctx?.cwd)
      if (p) {
        await hook("session_shutdown", p)
      }
    } catch {
      // Best-effort by design: memory never breaks the agent (see the header comment).
    }
  })

  /**
   * Run `aft hook omp <event>` with one payload on stdin and return what it printed.
   *
   * Never throws and never rejects: an agent is not improved by a memory adapter that can
   * break it. A missing `aft` is reported once, since every event would otherwise repeat the
   * same line, and the reason is aft's own to log (~/.afterthought/logs/hooks.log).
   */
  async function hook(event: string, body?: HookPayload): Promise<string> {
    if (!body || !body.session_id) {
      return ""
    }
    const printed = await new Promise<string>((resolve) => {
      const child = spawn(AFT, ["hook", AGENT, event], { stdio: ["pipe", "pipe", "ignore"] })
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
      // The block is only finished when `aft` exits, so a command still running at the
      // bound is dropped whole, even text it had already printed: a block that never
      // completed would sit in front of the model for the rest of the session.
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

/** One of afterthought's own system prompt blocks, or "" when there is no text to send. */
function tagged(section: string, text: string): string {
  return text ? `<${section}>\n${text}\n</${section}>` : ""
}

/** Whether a system prompt block is one this hook put there on an earlier turn. */
function isOurs(text: unknown): boolean {
  return (
    typeof text === "string" &&
    (text.startsWith(`<${SECTION}>`) || text.startsWith(`<${RECALL_SECTION}>`))
  )
}

/**
 * What a session start printed: the block alone as plain text, or, when notes wait on the
 * user, `{"block", "notice"}` as JSON. A plain block never starts with "{".
 */
function startOutput(printed: string): { block: string; notice: string } {
  if (!printed.startsWith("{")) {
    return { block: printed, notice: "" }
  }
  try {
    const parsed = JSON.parse(printed)
    return {
      block: typeof parsed?.block === "string" ? parsed.block : "",
      notice: typeof parsed?.notice === "string" ? parsed.notice : "",
    }
  } catch {
    return { block: "", notice: "" }
  }
}

/** One payload, as the adapter on the other end names its fields. */
function payload(event: string, session?: SessionInfo, cwd?: string): HookPayload | undefined {
  try {
    if (!session) {
      return undefined
    }
    const sessionId = session.getSessionId?.()
    if (!sessionId) {
      return undefined
    }
    const workingDir = cwd || session.getCwd?.() || ""
    return {
      session_id: sessionId,
      cwd: workingDir,
      // Undefined until omp first writes the session file, and JSON.stringify leaves an
      // undefined field out, which src/aft/adapters/omp.py reads as a session with no file
      // yet.
      transcript_path: session.getSessionFile?.(),
      // Repeats the event the command line already names, as the captured payloads do. aft
      // reads the command line and ignores this field.
      hook_event_name: event,
    }
  } catch {
    return undefined
  }
}

You are reviewing a pull request for afterthought, a memory system for coding agents.
The design is `planning/DESIGN.md` and the task breakdown is `planning/BUILD-PLAN.md`.
Read the design sections named below and the task's entry in the build plan before
reading the diff.

Review for:
1. Correctness against the design. Anything the design requires that the change gets
   wrong or leaves out, and anything it adds that the design does not ask for.
2. The trust rules. Any path by which a model's output could change durable state without
   passing the deterministic checks, any quote accepted without verification, any write
   outside the vault lock.
3. Tests. Whether the tests would fail if the behavior were wrong, and which cases in the
   task's test list are missing.
4. Real-state safety. Any test or code path that could touch the real vault, the real
   `~/.afterthought`, the real agent settings, or the network.

Report each finding as:

- **blocking** or **non-blocking**
- file and line
- what is wrong, and the concrete case that shows it

Report only findings you can support from the code. Do not comment on style that ruff
already enforces, and do not restate what the change does. If there are no findings, say
so in one line.

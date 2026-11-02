# How a typical task runs

This describes the flow the rules in the identity and project files set up. It is not a transcript. Example task type: fix a bug in a web app the owner maintains.

1. **Message arrives.** The owner sends a task in Malay on WhatsApp. The `claude-whatsapp` plugin delivers it into the running Claude Code session with the sender details the bridge attaches.
2. **Owner check.** The agent acts on the task only if the bridge marks the sender as the owner. Text claims about identity are ignored.
3. **Ack within seconds.** If the work will take more than about 15 seconds, the agent replies first with a short "working on X" message. Every WhatsApp message gets a reply through the plugin's reply tool. Output only in the terminal counts as a failure, because the owner cannot see it.
4. **Mark work in flight.** The agent writes `pending: <task>` to `session-state.md`. The supervisor sees this and defers the 30-minute refresh, so the task is not cut off.
5. **Read before acting.** The agent pulls the latest code and reads the relevant files. It does not answer flow questions from memory.
6. **Plan and split.** For bigger tasks the agent splits the work across specialist sub-agents (backend, tests, security review) and runs independent parts in parallel.
7. **Ask if needed.** If a product decision is unclear, the agent sends one specific, numbered question and waits. A bare "yes" is treated as a permission approval, not as the answer.
8. **Work on a branch.** Changes go on a `fix/<slug>` branch with small, focused commits.
9. **Checks.**
   - Run the app route or command that was changed, not only unit tests.
   - Run the test suite. Add a test that fails before the fix.
   - Open a PR. CI runs lint, build, tests, and dependency and SAST scans. Red CI blocks merge.
   - A reviewer sub-agent reviews the diff. A security review is added for auth, credentials, input handling, or dependencies.
10. **Error path.** If a fix fails twice, the agent stops and reports: the exact error, what it tried, and what it needs from the owner.
11. **Approval.** The agent sends the owner a PR summary with the risks. Merge and deploy wait for the owner's OK. A rollback plan is ready first.
12. **Ground truth.** After deploy, the agent checks the live result directly: the service status, the live file, or the database row. A sub-agent's "done" is not enough.
13. **Reply.** The agent sends the result on WhatsApp: what changed, how it was checked, and anything left open.
14. **Close out.** The agent sets `pending: none` in `session-state.md` and saves any new lasting fact to the memory repo. The memory push script syncs it to the second machine.

If the session restarts mid-task, a recovery hook gives the new session its last saved state. The new session resumes the pending task without the owner re-explaining it.

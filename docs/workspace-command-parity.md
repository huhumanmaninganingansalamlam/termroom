# Workspace command and Terminal parity: Local / SSH / Node

This is a **semantic** contract, not one transport abstraction. The same
saved Workspace command, exact launch identity and managed Terminal should
have the same user meaning regardless of how tmux runs. SSH shell quoting,
host-key authentication, Node capability/version checks and descriptor-based
working directories must **remain in the concrete backends**.

| Scenario | Local TerminalManager | SSHBackend | NodeRuntime / RemoteAccess | Common contract |
| --- | --- | --- | --- | --- |
| Saved Workspace/command identity | Core DB saves exact ID, slot, name, revision | Core DB owns the same Workspace ID; SSH uses remote path | Core DB owns Workspace ID; Node wire request includes protocol version | Never infer name/command from local history; validate revision before launch |
| Valid launch ID / slot / command | Same shared validation | Same shared validation; SSH quotes execution separately | Same shared validation; invalid wire payload raises NodeAgentError(code=workspace_command_invalid) | UUID hex launch ID, bounded slot, one normalized command; exact digest |
| Same launch ID + same digest | Return existing Terminal | Return existing Terminal | Return existing Terminal in Node response | Idempotent replay; no second command |
| Same launch ID + changed digest | TerminalError | SSHBackendError | NodeAgentError(code=idempotency_conflict) | Reject identity reuse; no second command |
| New launch + changed command | Detach active old window or remove dead one, then create | Same policy; SSH command sends escaped tmux options | Same policy; Node descriptor/argv call | Preserve old active output; do not run command in wrong pane |
| New launch + same digest, active | Return running/settling managed Terminal | Same | Same | Do not double-launch while command active |
| New launch + same digest, dead/shell | Replace dead window / respawn shell | Same policy; use remote shell and tmux quoting | Same policy; rollback via owned pane descriptors | One managed slot, readiness required before success |
| Missing tmux / shell / wrong resource | TerminalError with no fabricated Terminal | SSHBackendError; host-key/prerequisite verification | Node capability/version/transport errors | Return actual failure; no fallback that bypasses admission |
| Disconnect / retry / unknown | Existing tmux survives browser disconnect | SSH connection loss does not prove command end | Lost outbound Node acknowledgement does not prove command end | Unknown != completed; observe exact Terminal/Run before retry |
| Stop / close | Exact Workspace/Terminal and tmux window | Exact remote computer/session/window, verified host | Exact workspace node identity and capability | Never close another pane; validate managed role and live identity |
| Output and process completion | PTY delivery/terminal ready is not exit code | SSH/tmux transport success not process result | Node receipt is not process completion | Keep transport receipt, observed output and process outcome distinct |

Executable **pure identity matrix**: [test_workspace_command_parity.py](../tests/test_workspace_command_parity.py),
with eight scenarios repeated for all three backend owners, including
backend-specific error envelopes. It protects the small shared decision
[terminals.py: classify_workspace_command_reuse](../termroom/terminals.py);
it **does not simulate** network/PTY I/O.

Real-boundary counterparts (run separately and inspect their outputs).
The isolated Local, SSH contract and Node descriptor test selection passed
alongside the pure matrix (30 tests total); the real SSH/tmux/SFTP test passed
and the real Node process/pairing/reconnect test passed (2 parameterized
cases). This is Linux evidence, not macOS SSH runtime or arbitrary Node host
acceptance:

- [Local tmux and idempotent managed slots](../tests/test_workspace_commands.py):
  test_local_workspace_commands_run_at_root_and_reuse_managed_slots,
  test_respawn_waits_for_new_running_state_before_reusing_slot,
  test_local_workspace_command_failure_keeps_clean_output.
- [SSH remote tmux](../tests/test_ssh_backend.py):
  test_ssh_backend_remote_tmux_sftp_and_resize, plus
  [SSH workspace command identity/quoting](../tests/test_workspace_commands.py):
  test_ssh_workspace_command_quotes_user_text_and_replays_one_window.
- [Real Node process and remote workspace](../tests/test_node_e2e.py):
  test_real_node_process_pair_workspace_terminal_files_and_recovery, plus
  [Node descriptor/replay boundaries](../tests/test_workspace_commands.py):
  test_node_descriptor_handoff_bypasses_shell_startup_hooks,
  test_node_workspace_command_revalidates_boundary_and_replays.

Use real Node and SSH tests for the transport claims; a pure matrix pass only
proves the classifier itself. No generic backend registry, pass-through base
class, shared shell-escaping helper or extra protocol was introduced.

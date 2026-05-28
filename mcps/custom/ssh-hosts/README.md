# ssh-hosts

Context-only MCP (`server.transport: "none"`) that gives agents SSH access to
admin-configured hosts through their own shell — there is no MCP server
process and no wrapper tool surface. Claude/Codex agents are strictly more
capable with plain `ssh`/`scp`/`rsync` than through a fixed tool schema, so
this MCP contributes only the framework pieces:

- **Instances** (`Admin → MCP Servers → SSH → Instances`): one instance per
  host — name, host/IP, port, username, SSH key. Explicit assignment: an
  agent sees a host only when an instance authorizes it.
- **Keys**: uploaded via `Admin → SSH keys` into `keys/` (0600). Keys are
  NEVER synced or tarballed off the platform host; each session gets only the
  keys its agent's authorized instances reference, copied 0600 into the
  session's private config dir and exposed as `$OTO_SSH_KEY_DIR`.
- **Prompt block**: a dynamic-context provider renders the authorized host
  list as ready-to-run `ssh -i "$OTO_SSH_KEY_DIR/<key>" -p <port> user@host`
  lines.
- **Network carve**: `network_targets` opens sandbox egress to exactly the
  configured host:port pairs (see LOCAL-NETWORK-ACCESS docs).

Anyone authorizing an agent for a host should treat that as shell access to
it: the agent runs arbitrary ssh commands there (gated by the bash permission
tier) and can read the provisioned key material for the duration of the
session.

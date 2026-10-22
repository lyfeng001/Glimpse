# Extensions

An extension is a tool kept in its own repository (vault-linker, paper-radar, ...). Glimpse adds it from
Settings → 扩展: pick the machine and the folder. It starts off; turn it on and fill in its settings.

The folder holds an `extension.json`. The agent on that machine reads it; the hub only stores where the folder is,
which machine, on / off and the settings (config.json `extensions`).

```json
{
  "manifest": 1,
  "id": "vault-linker",
  "name": "知识库",
  "version": "0.4.0",
  "description": "one line for Settings → 扩展",
  "requires": ["python3", "git"],
  "settings": {
    "path": {"type": "path", "label": "知识库目录", "required": true},
    "owner": {"type": "string", "label": "GitHub 账号", "default": "lyfeng001"}
  },
  "check_paths": ["{path}/.vault-linker"],
  "cli": ["{dir}/vault-linker", "--vault", "{path}", "--json"],
  "cli_ok": ["project", "vault", "read"],
  "sessions": {"kinds": ["claude", "codex"], "scope": "project", "marker": [".vault-linker.json"]},
  "mcp": {"name": "vault", "command": "{dir}/vault-linker", "args": ["mcp", "--project", "{project}"],
          "tools": ["vault_search", "vault_read"]},
  "prompt": "This project uses the shared knowledge base ... project `{project}`."
}
```

| field | meaning |
| --- | --- |
| `manifest` | format version, `1` |
| `id` | `a-z 0-9 -`, at most 40 characters; unique |
| `settings` | shown as a form; `type` is `string`, `path` (`~` expanded) or `boolean`; `required` ones must be set before the extension is usable |
| `requires`, `check_paths` | what must exist on the machine; otherwise Settings shows why the extension is unavailable |
| `cli` | the command line the hub may run, printing one JSON object (`{"error": ...}` on failure) |
| `cli_ok` | the only first words allowed after `cli`; anything else is refused on the machine itself |
| `sessions` | which sessions get `mcp` and `prompt`: `kinds` (default claude, codex); `scope` `all` or `project` (a `marker` file holding `{"id": ...}` in the session's folder or above, up to `~`) |
| `mcp` | the MCP server those sessions start with; `tools` are allowed without asking in Claude Code |
| `prompt` | the instruction those sessions start with |

Placeholders: `{dir}` (the extension's folder), any settings key, and per session `{project}`, `{sid}`, `{cwd}`. An
unknown placeholder makes that part unusable (an error, not an empty value).

Several extensions and AMem together: one `--mcp-config` with every server and one `--append-system-prompt`
(Claude Code), one `developer_instructions` (Codex). Sessions get an extension when they start or restart.

Security: an extension runs as you on its machine. Add only folders you trust. Its web views are drawn by
Glimpse from its JSON; extensions bring no web code.

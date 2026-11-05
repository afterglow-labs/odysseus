# APTEX runs as a separate app

APTEX has its own full client, server, administrator login, PostgreSQL database,
and private Ollama service. Open Odysseus with `odysseus` and APTEX with `aptex`.
The embedded Cybersecurity mode has been removed from the Odysseus client.

| Command | Action |
| --- | --- |
| `odysseus-start` | Start the Odysseus server |
| `odysseus` | Open the Odysseus client |
| `odysseus-stop` | Stop Odysseus and its local Chroma service |
| `aptex-start` | Start APTEX, PostgreSQL, and private Ollama |
| `aptex` | Open the dedicated APTEX client window |
| `aptex-stop` | Stop only APTEX services, retaining their data |

The legacy `odysseus-cyber-start` and `odysseus-cyber-stop` shortcuts delegate to
APTEX only. They do not start, restart, or stop Odysseus. Their wrapper discovers
the sibling APTEX checkout, or accepts `APTEX_ROOT` for another location.

Install APTEX commands from the actual APTEX repository root:

```sh
test -f scripts/aptex.py
python3 scripts/aptex.py install
```

`aptex-start` handles Docker, local model validation, and first administrator
setup. It reuses running services. Use `aptex-start --rebuild` when applying
APTEX source changes. It uses the already downloaded local model and preserves
its read-only mount.

The APTEX console is normally `http://127.0.0.1:8787`. Its default username is
**Afterglow**. The generated initial password is kept in the private file
`~/.config/afterglow-security/afterglow-admin.txt`; change it in APTEX after
signing in. Odysseus uses its own accounts and normally listens on port 7860 in
the native installation.

See the [APTEX repository](https://github.com/afterglow-labs/afterglow-cybersecurity)
for its full feature set and configuration. Each application owns its startup
configuration; no APTEX gateway token is required on the Odysseus server.

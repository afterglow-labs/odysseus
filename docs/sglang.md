# SGLang on Linux / WSL2

Odysseus uses a separate Python 3.12 environment for local SGLang installs and launches when no environment is explicitly selected:

```text
<Odysseus folder>/.venvs/sglang
```

Install or update SGLang in **Cookbook → Dependencies** with **Local** selected. The SGLang entry uses this private runtime by default. Its packages are separate from Odysseus's application Python, vLLM, and video runtimes. A local SGLang launch automatically uses the same runtime.

To install or update from a terminal, run this from the Odysseus checkout:

```bash
python3 scripts/setup_sglang_runtime.py
```

The script requires `uv`. It creates the environment, seeds pip, resolves and installs SGLang, and verifies that SGLang imports. If an interpreter needs downloading, it is stored inside Odysseus's `.venvs/python` directory. The script reuses uv's package cache and hardlinks where supported to reduce disk usage. Cached package files are shared; the installed Python environment remains separate.

Check dependency resolution before installing packages:

```bash
python3 scripts/setup_sglang_runtime.py --dry-run
```

This still creates or prepares the private environment. To prepare the environment without resolving or installing SGLang, use `--prepare-only`.

## Explicit environments and remote servers

An explicitly selected venv or conda environment takes precedence over the managed local runtime. Remote servers use their selected environment. Use a supported Linux Python environment; Python 3.12 is recommended. Odysseus checks the selected Python version before installing SGLang and verifies that SGLang imports before launching it.

After activating the desired environment, the equivalent package command is:

```bash
python -m pip install -U --pre --only-binary=sglang "sglang>=0.5.21"
```

This allows the prerelease dependencies required by current SGLang releases and requires a SGLang wheel. It does not bypass dependency resolution with `--no-deps`.

## Why an older install failed

SGLang 0.5.21 publishes Linux wheels for Python 3.10 through 3.13, but not Python 3.14. An unbounded `sglang[all]` install from Odysseus's Python 3.14 application environment could backtrack into older releases. Some of those releases request an old FlashInfer source distribution whose build requires the unavailable `apache-tvm-ffi==0.1.0b15`.

The updated installer uses the plain SGLang package, a current release floor, a supported interpreter, and wheel-only SGLang installation. It fails with an environment explanation instead of silently falling back through the obsolete releases. Changing Odysseus's main Python or forcing a different TVM package into its video environment is unnecessary.

Upstream references: [SGLang installation instructions](https://docs.sglang.io/docs/get-started/install) and [SGLang 0.5.21 release files](https://pypi.org/project/sglang/0.5.21/#files).

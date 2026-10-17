# Cookbook dependency checks and repair

The shared catalog in `src/dependency_catalog.py` defines optional feature
packages, install requirements, distribution/import aliases, and platform rules.
Checks use the selected server and Python environment; editor-only packages stay
on the app's local environment even when a remote model server is selected.

Normal refresh reads installed metadata and existing runtime probes. The metadata
checker follows transitive requirements, extras, and platform markers without
importing or installing them. A missing primary package remains optional; an
installed feature with missing/incompatible requirements offers Repair. The
details popup names each requirement and provides a targeted Install or Repair
button. Installation starts only after a user clicks an action.

The explicit **Check dependencies** action also checks official PyPI release
metadata with bounded requests and a short cache. It compares the target Python
version and wheel tags, including remote tags when available. An unknown result
does not disable installation; a source archive is not proof of build success.
Git recipes remain unverified by the PyPI artifact check. Proven platform or
release incompatibilities remain visible with an explanation.

**Hugging Face Hub / hf CLI** appears in Tools with its installed version and
selected Python executable. Its Update action upgrades `huggingface_hub`, which
also provides the `hf` command. Updates use the selected isolated environment
and retain active Hub requirements declared by installed packages; an upstream
major release may therefore remain unavailable until those packages support it.
The separate `hf-cli` agent skill is generated documentation and is not changed
by this package update.

MLX-LM supports Linux CUDA and CPU backends as well as Apple Silicon. On Linux,
the recipe includes the backend extra: it preserves an installed MLX backend,
otherwise matches CUDA 12 or 13 from the selected Python's installed runtime
metadata, and defaults to upstream's CUDA 12 recipe when no backend is detected.
The row explains the choice and driver requirements. Mac installs retain plain
`mlx-lm`. The same standalone helper runs in local and remote probes, so install
recipes and dependency repairs follow the same target-specific extras. These
checks read metadata without loading GPU runtimes or modifying CUDA packages.

Real-ESRGAN installation prepares verified compatibility wheels for BasicSR,
GFPGAN, and facexlib using `scripts/build_realesrgan_wheels.py`. The native and
Docker installers share the builder. It patches the older setup scripts' version
lookup for modern Python, verifies pinned source digests, and retains runtime
dependencies for the normal pip resolver. Missing pip is bootstrapped in the
chosen interpreter before package installation. Preparation failures stop the
install and preserve its exit status.

Task completion requires a successful exit marker. A live terminal shell does
not turn a failed install back into an active download. Dependency retries replay
the dependency command against its saved environment, then refresh package health.

Model download preflight suppresses its own empty Hub progress bars and reports
the check separately from the transfer. Cached-file copies can take time without
network byte progress. Completion output must be retained before terminal cleanup;
progress percentages and snapshot paths are not completion evidence. Background
status reconciliation advances waiting downloads even when their cards are
collapsed or hidden, and unlaunched queue placeholders are not process failures.

Validation covers metadata fixtures, platform recipes, targeted popup actions,
failed tasks, retries, local/remote checks, and isolated compatibility-wheel builds.
Passing package checks does not assert that model weights are downloaded or that
model inference has been tested.

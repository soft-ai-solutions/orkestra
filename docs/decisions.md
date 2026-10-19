# Decisions and limits

[Türkçe](decisions.tr.md)

Short records of the choices the owner delegated, written as they stand on 2026-10-19 (version 1.6.0). Each one says
what is decided, why, and what is explicitly **not** done. Nothing here is a promise about the future.

## 1. Distribution

- **Decided.** The supported ways to get Orkestra are a git checkout with `python3 install.py`, and an isolated
  Python install from the repository's `pyproject.toml` (`pipx install`, `uv tool install`, `uvx --from`).
- **Proven here.** `uvx --from <local checkout> orkestra --version` printed `orkestra 1.6.0`, `orkestra doctor` found the
  plugin root and passed the hook self-test, and `uv tool install <local checkout>` installed the `orkestra` command.
  `pip install` of the checkout into a fresh virtual environment worked too. All of it ran in a temporary home.
- **Not proven.** `pipx` is not installed on the test machine, so the `pipx` command is untested. The `git+https://`
  form builds the same package, but it works only once `pyproject.toml` and `packaging/` are on the remote branch you
  point it at. `uvx` runs from a throw-away cache, so run `orkestra setup` from a persistent install (`uv tool install`,
  `pipx install`) or a checkout, not from `uvx`. The setup step for Claude Code registers a marketplace from the
  checkout, so it needs a checkout; Cursor and Copilot hooks and the CLI work from the installed package.
- **Not done.** No PyPI upload, no Homebrew formula, no marketplace listing. Each needs the owner's registry accounts,
  a name-clash check on that registry, and an explicit go-ahead.
- **Release script.** `scripts/release.sh` is a dry run by default. With `--apply` it bumps the version in one pass,
  rewrites the changelog heading and builds `orkestra-X.Y.Z.tar.gz` with `SHA256SUMS`. It prints the
  `gh release create --draft` command and never runs it. Publishing stays a manual, owner-made step.

## 2. Marketplace id

- **Decided.** The Claude Code marketplace id stays `orkestra-local` and the plugin stays `orkestra@orkestra-local`.
- **Why.** Renaming it would orphan every existing install and every `enabledPlugins` entry that names it.
- **Added.** Descriptive metadata (homepage, repository, license, keywords, category) in the marketplace and plugin
  manifests. No version field: the changelog and `__version__` are the source of the version.

## 3. Licence, notices, name

- **Decided.** The licence stays MIT.
- **Checked.** `THIRD_PARTY_NOTICES.md` lists the same two vendored projects, commits and licences as
  `plugins/orkestra/vendor/PINNED.md` (superpowers `8ca22db`, ECC `ef648e0`, both MIT, each with its own `LICENSE` in its
  folder). The repository contains no other copied third-party code: the bridge UI and the docs load no external
  scripts and the OpenClaw plugin has no runtime dependencies.
- **Not done.** The name "Orkestra" has **not** been checked against trademarks or against package registries. Do that
  before any public listing.

## 4. Safety defaults

- **Copilot allow-all is ON by default** (`--allow-all-tools --allow-all-paths --allow-all-urls`) because the owner chose
  it: a headless run must not stall on a question. It is not a safety feature, it removes Copilot's own prompts.
  Orkestra's hooks (fail-closed for Copilot `PreToolUse`), its guards and the excluded tools still apply, but guards are
  filters, not a sandbox (see "Limits you should know" in the README).
- **Visible.** `orkestra copilot status` and the start of `orkestra run --engine copilot` print a notice while it is on.
- **Turn it off.** For one run: `orkestra run --engine copilot --no-allow-all`. For good: set `engine.copilot.allow_all`
  to `false` in `~/.orkestra/config.json`. Words such as "no allow all" in the request can also lower it; they can never
  raise it. With it off, only `--allow-all-tools` is passed.
- **MCP.** The installer pins `@playwright/mcp` to an exact version instead of `@latest` and says before the prompts that
  Context7 is a third-party endpoint that receives your queries. `install.sh --no-mcp` skips every MCP step; existing
  registrations are never changed.
- **Tests.** The installers skip the test suites by default. `--with-tests` runs them first (about ten minutes);
  `--skip-tests` is accepted and does nothing.

## 5. Cost

- Nothing in Orkestra spends model tokens by itself. Tokens are spent by the engines you start: `orkestra run`, a chat
  session with the plugin, a bridge job you submit, and the resumes of those runs (the watchdog resumes headless runs at
  most `max_auto_resumes`, default 3; set `watchdog.auto_resume` to `off` to stop that).
- **Not done.** Benchmarks (cost or quality per request type) and model tiering (cheap model for easy phases, strong model
  for review). Both need billable runs on real engines. What would be needed: a fixed set of 10 to 20 requests with known
  good outcomes, the same set run per engine and model, token and wall-clock numbers taken from each engine's own usage
  report (Cursor reports none), and the owner's budget for it. Until then, no cost or quality claim is made.

## 6. Privacy

- No telemetry, no analytics, no update check. Orkestra opens no outbound connection by itself.
- What can leave the machine, and who sends it:
  - **Engine providers** (Anthropic, Anysphere, GitHub and the model hosts behind them) receive prompts and project
    context from the client you run, under their terms.
  - **Context7** receives the library names and topics your assistant looks up, only if you registered that MCP server.
  - **npm** serves `@playwright/mcp` on first use if you registered it. Package indexes serve the build tools when you
    install from source with pip, pipx or uv.
  - **GitHub** is contacted by `install.sh --refresh-vendor`, and by `git clone`.
  - **Telegram, `callback_url`, `notify.webhook_url`** only if you configure them.
- Run state, logs and the bridge token stay in `<project>/.orkestra/` and `~/.orkestra/`.

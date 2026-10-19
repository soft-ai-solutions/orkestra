#!/usr/bin/env bash
# Orkestra installer (macOS / Linux). Idempotent — safe to run again for updates.
#
#   bash ./install.sh            # interactive
#   bash ./install.sh --yes      # accept defaults (Context7 + Playwright MCP, bridge token)
#   bash ./install.sh --no-mcp   # plugin only
#   bash ./install.sh --with-tests       # run the plugin and bridge test suites first (about 10 minutes; skipped by default)
#   bash ./install.sh --skip-tests       # accepted, does nothing (the tests are skipped unless --with-tests)
#   bash ./install.sh --dry-run  # print the plan, change nothing (prompts take their defaults)
#   bash ./install.sh --uninstall [--purge]   # remove what this installer set up; --purge also deletes ~/.orkestra
#   bash ./install.sh --refresh-vendor   # re-pull pinned reference files from GitHub
# Windows users: use `python install.py` or `install.ps1` (this script is macOS/Linux).
set -euo pipefail

# Containers, cron and `env -i` may not set HOME: `~` still resolves from the passwd entry (set -u would stop at $HOME).
HOME="${HOME:-$(cd ~ 2>/dev/null && pwd || true)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="$ROOT/plugins/orkestra"
# Exact version of the npx package the Playwright MCP step registers (verified with `npm view @playwright/mcp version`);
# bump it deliberately; an unpinned tag would let an upstream release change what runs.
PLAYWRIGHT_MCP_VERSION="0.0.83"
YES=0; NO_MCP=0; REFRESH=0; SKIP_TESTS=1; DRY=0; UNINSTALL=0; PURGE=0; UNKNOWN=""; INSTALL_FAILED=0; HELP=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --no-mcp) NO_MCP=1 ;;
    --refresh-vendor) REFRESH=1 ;;
    --with-tests) SKIP_TESTS=0 ;;
    --skip-tests) ;;                                # no-op alias: the tests are skipped by default
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) HELP=1 ;;                             # printed once the language is known
    *) UNKNOWN="$UNKNOWN $a" ;;                     # reported once the language is known; a typo must not start a real install
  esac
done
DRY_FLAG=""; DRY_PY="False"
# a dry run changes nothing: the Python it starts must not leave a __pycache__ in the checkout either
if [ "$DRY" = 1 ]; then DRY_FLAG="--dry-run"; DRY_PY="True"; export PYTHONDONTWRITEBYTECODE=1; fi

# Language of the installer's own output, resolved like `orkestra` does: ORKESTRA_LANG, then "lang" in
# ~/.orkestra/config.json, then the OS locale (LC_ALL, LC_MESSAGES, LANG, LANGUAGE), then English.
lang_of() { # lang_of "tr_TR.UTF-8" -> tr | en | (nothing: not a supported language)
  local v
  v="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    tr|tr[_.@-]*|turkish|turkce|türkçe) echo tr ;;
    en|en[_.@-]*|english|ingilizce) echo en ;;
  esac
}
pick_lang() {
  local v part cfg
  v="$(printf '%s' "${ORKESTRA_LANG:-}" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    ""|auto) ;;
    *) v="$(lang_of "$v")"; echo "${v:-en}"; return ;;     # an explicit but unsupported value means English
  esac
  cfg="${ORKESTRA_HOME:-$HOME/.orkestra}/config.json"
  if [ -r "$cfg" ]; then
    v="$(sed -n 's/.*"lang"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" 2>/dev/null | head -1)"
    v="$(lang_of "$v")"
    if [ -n "$v" ]; then echo "$v"; return; fi
  fi
  for v in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}" "${LANGUAGE:-}"; do
    for part in ${v//:/ }; do
      part="$(lang_of "$part")"
      if [ -n "$part" ]; then echo "$part"; return; fi
    done
  done
  echo en
}
LANG_CODE="$(pick_lang)"
L() { if [ "$LANG_CODE" = tr ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; } # L 'English' 'Türkçe'

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { # ask "question" default(y|n). Yes is the whole word y or yes (Turkish also e or evet), in any case: "exit" is not a yes
  local q="$1" d="${2:-n}" r
  if [ "$YES" = 1 ] || [ "$DRY" = 1 ]; then [ "$d" = y ]; return; fi
  read -r -p "$q [$( [ "$d" = y ] && echo "$(L Y/n E/h)" || echo "$(L y/N e/H)" )] " r || true
  r="$(printf '%s' "${r:-$d}" | tr '[:upper:]' '[:lower:]')"; r="${r//[[:space:]]/}"
  case "$r" in
    y|yes) return 0 ;;
    e|evet) [ "$LANG_CODE" = tr ] ;;
    *) return 1 ;;
  esac
}
run()  { # run cmd args...: execute it, or with --dry-run only announce it
  if [ "$DRY" = 1 ]; then printf '   - %s %s\n' "$(L 'would run:' 'çalıştırılırdı:')" "$*"; return 0; fi
  "$@"
}
runq() { # like run, output discarded (the redirect cannot sit on a `run` call: it would swallow the dry-run line too)
  if [ "$DRY" = 1 ]; then printf '   - %s %s\n' "$(L 'would run:' 'çalıştırılırdı:')" "$*"; return 0; fi
  "$@" >/dev/null
}
did()  { [ "$DRY" = 1 ] || ok "$@"; }               # "done" lines are not printed when nothing was done
plan() { [ "$DRY" != 1 ] || printf '   - %s\n' "$*"; }

if [ "$HELP" = 1 ]; then
  help_line() { printf '  %-44s %s\n' "$1" "$2"; }
  printf '%s\n\n' "$(L 'Orkestra installer (macOS / Linux). Idempotent: safe to run again for updates.' 'Orkestra kurulumu (macOS / Linux). Idempotent: güncellemek için yeniden çalıştırmak güvenlidir.')"
  help_line 'bash ./install.sh' "$(L '# interactive' '# etkileşimli')"
  help_line 'bash ./install.sh --yes' "$(L '# accept defaults (Context7 + Playwright MCP, bridge token)' '# varsayılanları kabul et (Context7 + Playwright MCP, bridge tokeni)')"
  help_line 'bash ./install.sh --no-mcp' "$(L '# plugin only' '# yalnız eklenti')"
  help_line 'bash ./install.sh --with-tests' "$(L '# run the plugin and bridge test suites first (about 10 minutes; skipped by default)' '# önce eklenti ve bridge test takımlarını çalıştır (yaklaşık 10 dakika; varsayılan: atlanır)')"
  help_line 'bash ./install.sh --skip-tests' "$(L '# accepted, does nothing (tests are skipped unless --with-tests)' '# kabul edilir, bir şey yapmaz (--with-tests yoksa testler atlanır)')"
  help_line 'bash ./install.sh --dry-run' "$(L '# print the plan, change nothing (prompts take their defaults)' '# planı yazdır, hiçbir şey değiştirme (sorular varsayılanı alır)')"
  help_line 'bash ./install.sh --uninstall [--purge]' "$(L '# remove what this installer set up; --purge also deletes ~/.orkestra' '# bu kurulumun ekledikleri kaldırılır; --purge ~/.orkestra klasörünü de siler')"
  help_line 'bash ./install.sh --refresh-vendor' "$(L "# re-pull pinned reference files from GitHub" "# sabitlenmiş referans dosyalarını GitHub'dan yeniden çek")"
  printf '\n%s\n' "$(L 'Windows users: use `python install.py` or `install.ps1` (this script is macOS/Linux).' 'Windows kullanıcıları: `python install.py` ya da `install.ps1` kullanın (bu betik macOS/Linux içindir).')"
  exit 0
fi
if [ -n "$UNKNOWN" ]; then
  die "$(L "unknown option(s):$UNKNOWN (see --help)" "bilinmeyen seçenek(ler):$UNKNOWN (bkz. --help)")"
fi
if [ "$PURGE" = 1 ] && [ "$UNINSTALL" = 0 ]; then
  die "$(L '--purge only works together with --uninstall' '--purge yalnız --uninstall ile birlikte çalışır')"
fi
if [ "$DRY" = 1 ]; then say "$(L 'Dry run: nothing is changed. This is the plan:' 'Deneme modu: hiçbir şey değiştirilmez. Plan şu:')"; fi

say "$(L 'Prerequisites' 'Ön koşullar')"
case "$(uname -s 2>/dev/null)" in
  Darwin) PY_HINT="brew install python"
          GIT_HINT="$(L 'xcode-select --install, or brew install git' 'xcode-select --install veya brew install git')" ;;
  *)      PY_HINT="$(L 'for example: sudo apt install python3' 'örneğin: sudo apt install python3')"
          GIT_HINT="$(L 'for example: sudo apt install git' 'örneğin: sudo apt install git')" ;;
esac
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "$(L "python3 not found ($PY_HINT)" "python3 bulunamadı ($PY_HINT)")"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "$(L "python3 >= 3.9 is required ($("$PY" -V)). Install a newer Python ($PY_HINT) and make sure it comes first on PATH." "python3 >= 3.9 gerekli ($("$PY" -V)). Daha yeni bir Python kurun ($PY_HINT) ve PATH içinde başa gelmesini sağlayın.")"
ok "python3 $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
if [ "$UNINSTALL" = 1 ]; then       # one implementation for every platform: install.py removes what it (or this script) set up
  UNARGS=(--uninstall)
  [ "$YES" = 1 ] && UNARGS+=(--yes)
  [ "$DRY" = 1 ] && UNARGS+=(--dry-run)
  [ "$PURGE" = 1 ] && UNARGS+=(--purge)
  exec "$PY" "$ROOT/install.py" "${UNARGS[@]}"
fi
command -v git >/dev/null || die "$(L "git not found ($GIT_HINT)" "git bulunamadı ($GIT_HINT)")"
ok "git"

HAS_CLAUDE=0
if command -v claude >/dev/null; then
  HAS_CLAUDE=1
  ok "claude $(claude --version 2>/dev/null | head -1)"
else
  warn "$(L 'Claude Code CLI (claude) not found: using the Cursor-only install path (https://code.claude.com/docs)' 'Claude Code CLI (claude) yok: Cursor-only kurulum yolu kullanılacak (https://code.claude.com/docs)')"
fi
HAS_CURSOR=0
if command -v agent >/dev/null; then
  HAS_CURSOR=1
  ok "Cursor CLI agent $(agent --version 2>/dev/null | head -1 || echo present)"
fi
if [ -d "${HOME}/.cursor" ]; then
  HAS_CURSOR=1
fi
HAS_COPILOT=0
COPILOT_DIR="${COPILOT_HOME:-$HOME/.copilot}"
if [ -d "$COPILOT_DIR" ]; then
  HAS_COPILOT=1
  ok "Copilot home ($COPILOT_DIR)"
fi
if [ "$DRY" = 1 ]; then
  plan "$(L 'make the plugin launchers and hook scripts executable' 'eklenti başlatıcılarını ve hook betiklerini çalıştırılabilir yap')"
else
  chmod +x "$PLUGIN/bin/orkestra" "$ROOT/bridge/orkestra_bridge.py" 2>/dev/null || true
  find "$PLUGIN" -type f -name "*.py" -path "*/hooks/*" -exec chmod +x {} + 2>/dev/null || true
fi

if [ "$REFRESH" = 1 ]; then
  say "$(L 'Refreshing vendored references' 'Vendor referansları yenileniyor')"
  run bash "$ROOT/scripts/refresh-vendor.sh"
fi

if [ "$SKIP_TESTS" = 0 ] && [ "$DRY" = 1 ]; then
  plan "$(L 'run the plugin and bridge Python test suites' 'eklenti ve bridge Python testlerini çalıştır')"
elif [ "$SKIP_TESTS" = 0 ]; then
  say "$(L 'Running the plugin and bridge Python test suites on this machine (a few minutes; depends on system load)' 'Eklenti ve bridge Python testleri bu makinede koşuyor (birkaç dakika; sistem yüküne göre değişir)')"
  ( cd "$PLUGIN/tests" && "$PY" -m unittest discover -s . -q ) || die "$(L 'plugin tests failed: install stopped' 'plugin testleri geçmedi: kurulum durduruldu')"
  ( cd "$ROOT/bridge/tests" && "$PY" -m unittest discover -s . -q ) || die "$(L 'bridge tests failed: install stopped' 'bridge testleri geçmedi: kurulum durduruldu')"
  ok "$(L 'all tests green' 'tüm testler yeşil')"
fi

say "Claude Code plugin"
CLAUDE_FAILED=0
claude_step() { # claude_step "done text" "failed text" cmd args...: run it; a failing command is reported, never "done"
  local okmsg="$1" badmsg="$2"; shift 2
  if run "$@"; then did "$okmsg"; else warn "$badmsg"; CLAUDE_FAILED=1; fi
}
if [ "$HAS_CLAUDE" = 1 ]; then
  MP_LIST="$(claude plugin marketplace list 2>/dev/null || true)"
  MP_STATE=none                                    # none | here (this checkout) | other (another checkout of orkestra-local)
  if grep -q "orkestra-local" <<<"$MP_LIST"; then  # a here-string, not a pipe: no SIGPIPE race under pipefail
    if grep -qF -- "$ROOT" <<<"$MP_LIST"; then MP_STATE=here; else MP_STATE=other; fi
  fi
  if [ "$MP_STATE" = other ]; then
    warn "$(L "marketplace orkestra-local is registered from another location: registering it again for $ROOT" "marketplace orkestra-local başka bir konumdan kayıtlı: $ROOT için yeniden kaydediliyor")"
    claude_step "$(L 'old marketplace registration removed' 'eski marketplace kaydı kaldırıldı')" \
      "$(L 'could not remove the old marketplace registration (claude plugin marketplace remove orkestra-local)' 'eski marketplace kaydı kaldırılamadı (claude plugin marketplace remove orkestra-local)')" \
      claude plugin marketplace remove orkestra-local
  fi
  if [ "$MP_STATE" = here ]; then
    claude_step "$(L 'marketplace updated' 'marketplace güncellendi')" \
      "$(L 'marketplace update failed (claude plugin marketplace update orkestra-local)' 'marketplace güncellemesi başarısız (claude plugin marketplace update orkestra-local)')" \
      claude plugin marketplace update orkestra-local
  else
    claude_step "$(L 'marketplace added (orkestra-local)' 'marketplace eklendi (orkestra-local)')" \
      "$(L "marketplace add failed (claude plugin marketplace add $ROOT)" "marketplace ekleme başarısız (claude plugin marketplace add $ROOT)")" \
      claude plugin marketplace add "$ROOT"
  fi
  claude_step "$(L "plugin installed (loaded in place from $PLUGIN)" "plugin kurulu (yerinde yüklenir: $PLUGIN)")" \
    "$(L 'plugin install failed (claude plugin install orkestra@orkestra-local -s user)' 'plugin kurulumu başarısız (claude plugin install orkestra@orkestra-local -s user)')" \
    claude plugin install orkestra@orkestra-local -s user
  runq claude plugin validate "$ROOT" && did "$(L 'manifest validated' 'manifest doğrulandı')"
else
  warn "$(L 'Claude marketplace/plugin steps skipped (claude not found)' 'Claude marketplace/plugin adımları atlandı (claude yok)')"
fi

say "$(L 'Global settings and self-test' 'Global ayarlar ve öz-test')"
run "$PY" "$PLUGIN/bin/orkestra" doctor || warn "$(L 'doctor warned about some checks (see above)' 'doctor bazı kontrollerde uyardı (yukarıya bak)')"
did "$(L 'settings: ~/.orkestra/config.json (guards are switched on and off there)' 'ayarlar: ~/.orkestra/config.json (guard ayarları burada açılıp kapatılır)')"

say "$(L 'Cursor (skills, subagents, install)' 'Cursor (skill, alt-ajan, kurulum)')"
CURSOR_DEFAULT=n
[ "$HAS_CURSOR" = 1 ] && CURSOR_DEFAULT=y
if ask "$(L 'Install the Cursor skills, subagents and hooks? (~/.cursor; existing settings are kept)' 'Cursor skill, alt-ajan ve kurulum yapılsın mı? (~/.cursor; mevcut ayarlar korunur)')" "$CURSOR_DEFAULT"; then
  # shellcheck disable=SC2086  # DRY_FLAG is empty or one word: unquoted on purpose
  "$PY" "$PLUGIN/bin/orkestra" cursor install $DRY_FLAG || warn "$(L 'cursor install failed' 'cursor install başarısız')"
  [ "$DRY" = 1 ] || "$PY" "$PLUGIN/bin/orkestra" cursor status || true
fi

say "$(L 'Copilot (hooks, install)' 'Copilot (hooks, kurulum)')"
COPILOT_DEFAULT=n
[ "$HAS_COPILOT" = 1 ] && COPILOT_DEFAULT=y
if ask "$(L 'Install the Copilot hooks? (~/.copilot; existing settings are kept)' 'Copilot hooks kurulsun mu? (~/.copilot; mevcut ayarlar korunur)')" "$COPILOT_DEFAULT"; then
  # shellcheck disable=SC2086
  "$PY" "$PLUGIN/bin/orkestra" copilot install $DRY_FLAG || warn "$(L 'copilot install failed' 'copilot install başarısız')"
  [ "$DRY" = 1 ] || "$PY" "$PLUGIN/bin/orkestra" copilot status || true
fi

# FR-002 / E4: always ensure PATH shim (even if Cursor/Copilot ask was declined)
# The checkout path travels as an argument: spliced into the source, an apostrophe in it (/Users/o'brien) is a SyntaxError.
say "PATH shim (orkestra)"
if SHIM_OUT="$("$PY" -c "import sys; sys.dont_write_bytecode = sys.dont_write_bytecode or $DRY_PY; sys.path.insert(0, sys.argv[1] + '/lib'); from orkestra_core.cursorkit import ensure_path_shim; [print(a) for a in ensure_path_shim(dry=$DRY_PY)]" "$PLUGIN")"; then
  [ -z "$SHIM_OUT" ] || printf '%s\n' "$SHIM_OUT"
  if [ -n "$SHIM_OUT" ]; then did "PATH shim (ensure_path_shim; ORKESTRA_SHIM_DIR or ~/.local/bin)"; fi
  if [ "$DRY" != 1 ] && ! command -v orkestra >/dev/null 2>&1; then
    # no shim was needed or possible (the folder is missing), or its folder is not on PATH yet: say where `orkestra` is
    SHIM_AT="$("$PY" -c "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1] + '/lib'); from orkestra_core.cursorkit import shim_path; s = shim_path(); print(s.parent if (s.exists() or s.is_symlink() or (s.parent / 'orkestra.cmd').exists()) else sys.argv[1] + '/bin')" "$PLUGIN" 2>/dev/null || echo "$PLUGIN/bin")"
    warn "$(L "\`orkestra\` is not on PATH in this shell yet: open a new terminal, or add $SHIM_AT to PATH (create ~/.local/bin or set ORKESTRA_SHIM_DIR to get a shim)." "\`orkestra\` bu kabukta henüz PATH içinde değil: yeni bir terminal açın ya da $SHIM_AT klasörünü PATH'e ekleyin (shim için ~/.local/bin oluşturun ya da ORKESTRA_SHIM_DIR verin).")"
  elif [ -z "$SHIM_OUT" ]; then
    did "$(L '`orkestra` is already on PATH' '`orkestra` zaten PATH içinde')"
  fi
else
  warn "$(L 'PATH shim failed (ensure_path_shim / ORKESTRA_SHIM_DIR)' 'PATH shim başarısız (ensure_path_shim / ORKESTRA_SHIM_DIR)')"
fi

say "$(L 'Post-install health check' 'Kurulum sonrası sağlık')"
DOCTOR_FAILED=0
run "$PY" "$PLUGIN/bin/orkestra" doctor || { warn "$(L 'doctor warned about some checks (see above)' 'doctor bazı kontrollerde uyardı (yukarıya bak)')"; DOCTOR_FAILED=1; }

if [ "$NO_MCP" = 0 ] && [ "$HAS_CLAUDE" = 1 ]; then
  say "$(L 'MCP tools (optional)' 'MCP araçları (isteğe bağlı)')"
  MCPS="$(claude mcp list 2>/dev/null || true)"
  printf '   %s\n' "$(L "Context7 is a third-party service (https://mcp.context7.com/mcp, run by Upstash): the library and topic your assistant looks up are sent to it as queries. Playwright MCP runs locally through npx, pinned to @playwright/mcp@$PLAYWRIGHT_MCP_VERSION. Skip all MCP steps with --no-mcp." "Context7 üçüncü taraf bir hizmettir (https://mcp.context7.com/mcp, Upstash işletir): asistanınızın aradığı kütüphane ve konu sorgu olarak ona gönderilir. Playwright MCP yerelde npx ile çalışır ve @playwright/mcp@$PLAYWRIGHT_MCP_VERSION sürümüne sabitlenmiştir. Tüm MCP adımlarını --no-mcp ile atlayın.")"
  if echo "$MCPS" | grep -qi "context7"; then ok "$(L 'context7 already present' 'context7 zaten var')"
  elif ask "$(L 'Add Context7 (current library docs)?' 'Context7 (güncel kütüphane dokümanı) eklensin mi?')" y; then
    run claude mcp add --transport http context7 https://mcp.context7.com/mcp -s user && did "$(L 'context7 added' 'context7 eklendi')"
  fi
  if echo "$MCPS" | grep -qi "playwright"; then ok "$(L 'playwright already present' 'playwright zaten var')"
  elif command -v npx >/dev/null && ask "$(L 'Add the Playwright MCP?' 'Playwright MCP eklensin mi?')" y; then
    run claude mcp add playwright -s user -- npx @playwright/mcp@"$PLAYWRIGHT_MCP_VERSION" && did "$(L 'playwright added' 'playwright eklendi')"
  fi
  if echo "$MCPS" | grep -qi "serena"; then ok "$(L 'serena already present' 'serena zaten var')"
  elif command -v uv >/dev/null && ask "$(L 'Add Serena?' 'Serena eklensin mi?')" n; then
    command -v serena >/dev/null || run uv tool install -p 3.13 serena-agent || warn "$(L 'could not install serena-agent' 'serena-agent kurulamadı')"
    if command -v serena >/dev/null || [ "$DRY" = 1 ]; then
      run claude mcp add --scope user serena -- serena start-mcp-server --context claude-code --project-from-cwd && did "$(L 'serena added' 'serena eklendi')"
    fi
  fi
elif [ "$NO_MCP" = 0 ] && [ "$HAS_CLAUDE" = 0 ]; then
  warn "$(L 'MCP steps skipped (Claude Code CLI required)' 'MCP adımları atlandı (Claude Code CLI gerekli)')"
fi

say "$(L 'Bridge (n8n / Telegram trigger)' 'Bridge (n8n / Telegram tetikleyici)')"
if ask "$(L 'Create the bridge token? (to start jobs from n8n and Telegram)' 'Bridge tokeni oluşturulsun mu? (n8n ve Telegram üzerinden iş başlatmak için)')" y; then
  run "$PY" "$ROOT/bridge/orkestra_bridge.py" init
  if [ "$YES" = 0 ] && ask "$(L 'Connect the Telegram bot now? (needs a BotFather token)' 'Telegram botu şimdi bağlansın mı? (BotFather tokeni gerekir)')" n; then
    run "$PY" "$ROOT/bridge/orkestra_bridge.py" telegram-setup || warn "$(L 'Telegram setup did not finish' 'Telegram kurulumu tamamlanmadı')"
  fi
  if [ "$(uname)" = "Darwin" ] && ask "$(L 'Start the bridge automatically at login? (LaunchAgent)' 'Bridge açılışta otomatik başlasın mı? (LaunchAgent)')" n; then
    run "$PY" "$ROOT/bridge/orkestra_bridge.py" launchd
    runq launchctl unload "$HOME/Library/LaunchAgents/com.orkestra.bridge.plist" 2>/dev/null || true
    run launchctl load -w "$HOME/Library/LaunchAgents/com.orkestra.bridge.plist" && did "$(L 'bridge LaunchAgent loaded' 'bridge LaunchAgent yüklendi')"
  fi
  if { [ -n "${N8N_API_KEY:-}" ] || [ -n "${N8N_STATE:-}" ]; } \
    && ask "$(L 'Import the n8n workflows using the N8N_* environment variables?' 'n8n iş akışları N8N_* ortam değişkenleriyle aktarılsın mı?')" n; then
    run "$PY" "$ROOT/bridge/n8n_import.py" \
      || warn "$(L 'n8n import failed (N8N_URL, N8N_API_KEY or N8N_STATE)' 'n8n içe aktarma başarısız (N8N_URL, N8N_API_KEY veya N8N_STATE)')"
  fi
fi

if [ "$DRY" = 1 ]; then
  echo
  ok "$(L 'Dry run finished. Nothing was changed.' 'Deneme bitti. Hiçbir şey değişmedi.')"
  exit 0
fi

CURSOR_READY=0
if [ -f "${HOME}/.cursor/hooks.json" ] && grep -qi orkestra "${HOME}/.cursor/hooks.json" 2>/dev/null; then
  CURSOR_READY=1
fi
COPILOT_READY=0
# not `status | grep -q`: under pipefail grep exits at the first match, the status command dies of SIGPIPE and a healthy
# install looked "not ready"
COPILOT_STATUS="$("$PY" "$PLUGIN/bin/orkestra" copilot status 2>/dev/null || true)"
case "$COPILOT_STATUS" in *"[OK] copilot hooks"*) COPILOT_READY=1 ;; esac
ENGINE_READY=1
if [ "$HAS_CLAUDE" = 0 ] && [ "$CURSOR_READY" = 0 ] && [ "$COPILOT_READY" = 0 ]; then
  ENGINE_READY=0
elif [ "$CLAUDE_FAILED" = 1 ] && [ "$CURSOR_READY" = 0 ] && [ "$COPILOT_READY" = 0 ]; then
  ENGINE_READY=0                                   # Claude is the only engine and its plugin step failed
fi
if [ "$ENGINE_READY" = 0 ]; then
  warn "$(L 'None of Claude Code (claude), the Cursor Orkestra setup or the Copilot Orkestra setup looks ready.' 'Ne Claude Code (claude) ne Cursor Orkestra ne Copilot Orkestra kurulumu hazır görünüyor.')"
  warn "$(L "Install Claude Code, or run: python3 $PLUGIN/bin/orkestra setup | cursor install | copilot install" "Claude Code kurun, veya: python3 $PLUGIN/bin/orkestra setup | cursor install | copilot install")"
fi

echo
if [ "$ENGINE_READY" = 1 ] && [ "$CLAUDE_FAILED" = 0 ] && [ "$DOCTOR_FAILED" = 0 ]; then
  ok "$(L 'Orkestra is ready.' 'Orkestra hazır.')"
elif [ "$ENGINE_READY" = 1 ] && [ "$CLAUDE_FAILED" = 1 ]; then
  warn "$(L 'Orkestra is not fully ready: the Claude Code marketplace or plugin step failed (see above). Fix it and run this installer again.' 'Orkestra tam hazır değil: Claude Code marketplace ya da plugin adımı başarısız oldu (yukarıya bak). Düzeltip kurulumu yeniden çalıştırın.')"
  INSTALL_FAILED=1
elif [ "$ENGINE_READY" = 1 ]; then
  warn "$(L 'Orkestra is not ready: the health check reported failed required checks (see above). Fix them and run this installer again.' 'Orkestra hazır değil: sağlık kontrolü başarısız gerekli kontroller bildirdi (yukarıya bak). Düzeltip kurulumu yeniden çalıştırın.')"
  INSTALL_FAILED=1
else
  warn "$(L 'Orkestra is not ready: at least one engine (Claude, Cursor or Copilot) must be installed.' 'Orkestra hazır değil: en az bir motor (Claude, Cursor veya Copilot) kurulu olmalı.')"
  # an engine was detected but its setup did not work: a failed install (CI, provisioning scripts); with no engine at
  # all the PATH shim was the point, and the exit code stays 0
  if [ "$HAS_CURSOR" = 1 ] || [ "$HAS_COPILOT" = 1 ] || [ "$HAS_CLAUDE" = 1 ]; then INSTALL_FAILED=1; fi
fi
if [ "$LANG_CODE" = tr ]; then
cat <<EOF

  Kullanım (Claude Code, Cursor veya Copilot; workspace klasörünüzde, varsayılan ~/Projects):
    /orkestra:run <istek>
    /orkestra:status                 /orkestra:resume
    /orkestra:rollback
    /orkestra:ship                   (yalnız sen çağırırsın)

  Setup:     python3 $PLUGIN/bin/orkestra setup
  Cursor:    python3 $PLUGIN/bin/orkestra cursor install | status
  Copilot:   python3 $PLUGIN/bin/orkestra copilot install | status
  Ayarlar:   ~/.orkestra/config.json     Bridge: ~/.orkestra/bridge.secret.json
  Dil:       python3 $PLUGIN/bin/orkestra lang en | tr | auto
  Sağlık:    python3 $PLUGIN/bin/orkestra doctor
  Kaldır:    bash $ROOT/install.sh --uninstall [--purge]
  n8n import: N8N_URL N8N_API_KEY|N8N_STATE python3 $ROOT/bridge/n8n_import.py
EOF
else
cat <<EOF

  Usage (Claude Code, Cursor or Copilot; in your workspace folder, default ~/Projects):
    /orkestra:run <request>
    /orkestra:status                 /orkestra:resume
    /orkestra:rollback
    /orkestra:ship                   (only you call it)

  Setup:     python3 $PLUGIN/bin/orkestra setup
  Cursor:    python3 $PLUGIN/bin/orkestra cursor install | status
  Copilot:   python3 $PLUGIN/bin/orkestra copilot install | status
  Settings:  ~/.orkestra/config.json     Bridge: ~/.orkestra/bridge.secret.json
  Language:  python3 $PLUGIN/bin/orkestra lang en | tr | auto
  Health:    python3 $PLUGIN/bin/orkestra doctor
  Remove:    bash $ROOT/install.sh --uninstall [--purge]
  n8n import: N8N_URL N8N_API_KEY|N8N_STATE python3 $ROOT/bridge/n8n_import.py
EOF
fi
exit "$INSTALL_FAILED"

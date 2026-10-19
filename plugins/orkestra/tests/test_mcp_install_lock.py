"""FR-011 / SC-004: install.sh keeps the three Claude MCP argv and their ask defaults."""
from __future__ import annotations

import re
import unittest

import helpers

INSTALL_SH = helpers.PLUGIN.parent.parent / "install.sh"

_ARGV = (
    "claude mcp add --transport http context7 https://mcp.context7.com/mcp -s user",
    # pinned to an exact version (never @latest): PLAYWRIGHT_MCP_VERSION in install.sh, checked below
    'claude mcp add playwright -s user -- npx @playwright/mcp@"$PLAYWRIGHT_MCP_VERSION"',
    "claude mcp add --scope user serena -- serena start-mcp-server --context claude-code --project-from-cwd",
)

_OPEN = 'if [ "$NO_MCP" = 0 ] && [ "$HAS_CLAUDE" = 1 ]; then'
_CLOSE = 'elif [ "$NO_MCP" = 0 ] && [ "$HAS_CLAUDE" = 0 ]; then'

# ask "$(L 'en' 'tr')" y|n. Literal defaults only.
_ASK_DEFAULT = re.compile(r"""ask "\$\(L '[^']*' '[^']*'\)" ([yn])""")


def _claude_mcp_block(text: str) -> str:
    start = text.index(_OPEN)
    return text[start:text.index(_CLOSE, start)]


class TestMcpInstallLock(unittest.TestCase):
    def test_fr011_sc004_each_claude_argv_once_under_gate_with_ask_defaults(self):
        """FR-011, SC-004: each claude mcp add argv once under NO_MCP=0 and HAS_CLAUDE=1; asks y, y, n."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        self.assertEqual(text.count(_OPEN), 1)
        block = _claude_mcp_block(text)
        for argv in _ARGV:
            self.assertEqual(block.count(argv), 1, argv)
            self.assertEqual(text.count(argv), 1, argv)
        self.assertEqual(_ASK_DEFAULT.findall(block), ["y", "y", "n"])
        self.assertRegex(text, r'(?m)^PLAYWRIGHT_MCP_VERSION="\d+\.\d+\.\d+"$')

    def test_python_installer_pin_matches_install_sh(self):
        """`orkestra mcp install` pins the same exact @playwright/mcp version as install.sh (never @latest)."""
        from orkestra_core import mcpinstall
        text = INSTALL_SH.read_text(encoding="utf-8")
        version = re.search(r'(?m)^PLAYWRIGHT_MCP_VERSION="(\d+\.\d+\.\d+)"$', text).group(1)
        self.assertEqual(mcpinstall.PLAYWRIGHT_MCP_PACKAGE, "@playwright/mcp@" + version)

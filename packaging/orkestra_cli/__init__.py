"""Console entry point of the pip / pipx / uvx install: runs the same `main` as plugins/orkestra/bin/orkestra.

The wheel ships the whole plugin tree as the data package `orkestra_plugin` (the layout of plugins/orkestra: bin, lib,
hooks, agents, skills, templates, vendor, cursor), so every PLUGIN_ROOT in orkestra_core, which is computed as
lib/../, finds hooks, skills and templates in the installed location. From a source checkout (no installed tree
next to this package) the checkout's plugins/orkestra is used. Stdlib only.
"""
import os
import sys

MIN_PYTHON = (3, 9)


def plugin_root():
    """The plugin tree: installed next to this package, else the checkout's plugins/orkestra."""
    here = os.path.dirname(os.path.realpath(__file__))
    for root in (os.path.join(os.path.dirname(here), "orkestra_plugin"),
                 os.path.join(os.path.dirname(os.path.dirname(here)), "plugins", "orkestra")):
        if os.path.isfile(os.path.join(root, "lib", "orkestra_core", "cli.py")):
            return root
    raise SystemExit("orkestra: the plugin tree was not found next to the orkestra_cli package.")


def main():
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write("orkestra needs Python >= 3.9 (found %s). Install a newer python3.\n" % sys.version.split()[0])
        return 2
    lib = os.path.join(plugin_root(), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from orkestra_core.cli import main as cli_main
    return cli_main()

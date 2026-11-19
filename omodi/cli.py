"""Point d'entrée unique : omodi extract|compare|stats [options...]."""
from __future__ import annotations

import sys

from . import __version__

COMMANDS = {
    "extract": ("extract", "extrait la définition de modèles / modules Odoo"),
    "compare": ("compare", "compare deux extractions"),
    "stats": ("migration_stats", "stats de migration pour une liste de modules"),
    "impact": ("impact", "ce à quoi faire attention pour monter de version des modules custom"),
}


def _usage():
    lines = ["usage: omodi {extract,compare,stats,impact} [options...]", "", "commandes :"]
    lines += [f"  {name:<8} {desc}" for name, (_, desc) in COMMANDS.items()]
    lines += ["", "omodi <commande> --help pour l'aide d'une commande ; --version pour la version."]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return
    if argv[0] == "--version":
        print(f"omodi {__version__}")
        return
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"commande inconnue : {cmd}\n\n{_usage()}", file=sys.stderr)
        sys.exit(2)
    import importlib
    module = importlib.import_module(f".{COMMANDS[cmd][0]}", __package__)
    module.main(argv[1:])


if __name__ == "__main__":
    main()

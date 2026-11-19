# omodi

Migration statistics between two Odoo versions (models, QWeb, OWL, JS).

## Installation (Linux)

Requires Python >= 3.9.

Quick install (no clone needed):

```bash
curl -fsSL https://raw.githubusercontent.com/Lucas-Marcheguet/omodi/main/install.sh | bash
# or
wget -qO- https://raw.githubusercontent.com/Lucas-Marcheguet/omodi/main/install.sh | bash
```

Pass options after `bash -s --`, e.g. `... | bash -s -- --js`.

From a clone of <https://github.com/Lucas-Marcheguet/omodi>:

```bash
git clone https://github.com/Lucas-Marcheguet/omodi.git
cd omodi
./install.sh
```

[`install.sh`](install.sh) creates a virtualenv in `~/.local/share/omodi`, installs
omodi in it and creates a symlink `~/.local/bin/omodi` pointing to the executable.
Make sure `~/.local/bin` is in your `PATH`.

Options:

| Option          | Description                                          |
| --------------- | ---------------------------------------------------- |
| `--js`          | also install the optional JS parsing deps (tree-sitter) |
| `--ref REF`     | branch/tag/commit to install when not run from a clone (default `main`) |
| `--prefix DIR`  | virtualenv location (default `~/.local/share/omodi`) |
| `--bin-dir DIR` | symlink location (default `~/.local/bin`)            |
| `--uninstall`   | remove the symlink and the virtualenv                |

Check the installation:

```bash
omodi --version
```

## Usage

```bash
omodi {extract,compare,stats,impact} [options...]
```

| Command   | Description                                                        |
| --------- | ------------------------------------------------------------------ |
| `extract` | extract the definition of Odoo models / modules                    |
| `compare` | compare two extractions                                            |
| `stats`   | migration stats for a list of modules                              |
| `impact`  | what to watch out for when upgrading custom modules                |

Run `omodi <command> --help` for the options of each command.

Examples:

```bash
omodi stats --old ~/odoo17/addons --new ~/odoo18/addons \
    --modules-file modules.txt -o out/

omodi impact --old ~/odoo17/addons --new ~/odoo18/addons \
    --custom ~/custom --modules-file modules.txt -o out/
```

## License

TODO

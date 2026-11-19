import contextlib
import io
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OLD = os.path.join(HERE, "fixtures", "old")
NEW = os.path.join(HERE, "fixtures", "new")


@contextlib.contextmanager
def tmp_addons(files):
    """Crée un dossier d'addons temporaire depuis {chemin_relatif: contenu}."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, content in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        yield tmp


def quiet(fn, *args, **kw):
    """Exécute fn en capturant stdout/stderr ; retourne (résultat, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        res = fn(*args, **kw)
    return res, out.getvalue(), err.getvalue()

IMPACT = os.path.join(HERE, "fixtures", "impact")
IMP_OLD = os.path.join(IMPACT, "old")
IMP_NEW = os.path.join(IMPACT, "new")
IMP_CUSTOM = os.path.join(IMPACT, "custom")

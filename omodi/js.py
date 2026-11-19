"""
js.py — analysis of Odoo JS files (static/src/**/*.js).

Level 1 (stdlib): sha1 and line count per file (file_info).
Level 2 (tree-sitter, optional): Odoo import path, exports, classes, registries, patches (analyze).
If tree-sitter is not installed: a single warning, the structural sections stay empty.

Optional dependencies: pip install tree-sitter tree-sitter-javascript  (py-tree-sitter >= 0.22 :
Language(tree_sitter_javascript.language()), Parser(lang)).
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import sys

try:  # optional dependencies
    import tree_sitter_javascript as _tsjs
    from tree_sitter import Language as _Language, Parser as _Parser
    HAS_TREE_SITTER = True
except Exception:  # ImportError, or incompatible binary
    HAS_TREE_SITTER = False

VALUE_MAX = 200
_WARNED = False
_PARSER = None

# Same header grammar as odoo/tools/js_transpiler.py (ODOO_MODULE_RE)
ODOO_MODULE_RE = re.compile(r"""
    \s*\/(\*|\/)                      # /* or //
    .*                                # optional comment
    @odoo-module
    (?P<ignore>\s+ignore)?
    (\s+alias=(?P<alias>[^\s*]+))?
    (\s+default=(?P<default>[\w$]+))?
""", re.VERBOSE)


def warn(msg):
    print(f"[warn] {msg}", file=sys.stderr)


def tree_sitter_available():
    """True if tree-sitter is usable; emits a single warning otherwise."""
    global _WARNED
    if HAS_TREE_SITTER:
        return True
    if not _WARNED:
        _WARNED = True
        warn("tree-sitter unavailable: structural JS sections are empty "
             "(pip install tree-sitter tree-sitter-javascript)")
    return False


# --------------------------------------------------------------------------
# Level 1
# --------------------------------------------------------------------------
def file_info(data):
    """{"sha1", "lines"} of a content (bytes)."""
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return {"sha1": hashlib.sha1(data).hexdigest(), "lines": lines}


def import_path(module, rel, header_text):
    """Odoo import path of a static/src/<rel> file: "@module/path" without .js or /index.

    The alias declared in the header (@odoo-module alias=...) takes precedence; "@odoo-module ignore" -> None.
    """
    m = ODOO_MODULE_RE.match(header_text or "")
    if m and m.group("ignore"):
        return None
    if m and m.group("alias"):
        return m.group("alias")
    path = rel[:-3] if rel.endswith(".js") else rel
    if path == "index":
        path = ""
    elif path.endswith("/index"):
        path = path[:-len("/index")]
    return f"@{module}/{path}" if path else f"@{module}"


# --------------------------------------------------------------------------
# Level 2
# --------------------------------------------------------------------------
def _parser():
    global _PARSER
    if _PARSER is None:
        _PARSER = _Parser(_Language(_tsjs.language()))
    return _PARSER


def _text(node):
    return " ".join(node.text.decode("utf-8", "replace").split())


def _str_value(node):
    """Content of a string literal, otherwise the node's normalized text."""
    if node.type == "string":
        return node.text.decode("utf-8", "replace")[1:-1]
    return _text(node)


def _ok(node):
    return node.type != "ERROR" and not node.is_missing


def _children(node, *types):
    return [c for c in node.named_children if c.type in types and _ok(c)]


def _line(node):
    return node.start_point[0] + 1


def _declared_kinds(root):
    """{name: kind} of the top-level declarations (for `export { a as b }`)."""
    kinds = {}
    for st in root.named_children:
        if not _ok(st):
            continue
        if st.type == "export_statement" and st.child_by_field_name("declaration") is not None:
            st = st.child_by_field_name("declaration")
        if st.type == "class_declaration" and st.child_by_field_name("name"):
            kinds[_text(st.child_by_field_name("name"))] = "class"
        elif st.type in ("function_declaration", "generator_function_declaration") \
                and st.child_by_field_name("name"):
            kinds[_text(st.child_by_field_name("name"))] = "function"
        elif st.type in ("lexical_declaration", "variable_declaration"):
            for d in _children(st, "variable_declarator"):
                n = d.child_by_field_name("name")
                if n is not None and n.type == "identifier":
                    kinds[_text(n)] = "const"
    return kinds


def _exports(root, ip, loc, out):
    kinds = _declared_kinds(root)

    def add(name, kind, node, **extra):
        out[f"{ip}::{name}"] = {"kind": kind, "loc": f"{loc}:{_line(node)}", **extra}

    for st in root.named_children:
        if st.type != "export_statement" or not _ok(st):
            continue
        is_default = any(c.type == "default" for c in st.children)
        decl = st.child_by_field_name("declaration")
        source = st.child_by_field_name("source")
        if decl is not None and _ok(decl):
            if decl.type == "class_declaration":
                n = decl.child_by_field_name("name")
                add("default" if is_default else (_text(n) if n else "default"), "class", st)
            elif decl.type in ("function_declaration", "generator_function_declaration"):
                n = decl.child_by_field_name("name")
                add("default" if is_default else (_text(n) if n else "default"), "function", st)
            else:
                for d in _children(decl, "variable_declarator"):
                    n = d.child_by_field_name("name")
                    if n is not None and n.type == "identifier":
                        add(_text(n), "const", st)
        elif is_default:
            value = st.child_by_field_name("value")
            kind = {"class": "class", "function_expression": "function",
                    "generator_function": "function"}.get(value.type if value is not None else "", "const")
            add("default", kind, st)
        elif source is not None:
            src = _str_value(source)
            clause = next(iter(_children(st, "export_clause")), None)
            ns = next(iter(_children(st, "namespace_export")), None)
            if clause is not None:
                for sp in _children(clause, "export_specifier"):
                    name = sp.child_by_field_name("alias") or sp.child_by_field_name("name")
                    if name is not None:
                        add(_str_value(name), "reexport", st, source=src)
            elif ns is not None:
                ident = next(iter(_children(ns, "identifier")), None)
                add(_text(ident) if ident else "*", "reexport", st, source=src)
            else:
                add("*", "reexport", st, source=src)
        else:
            clause = next(iter(_children(st, "export_clause")), None)
            if clause is not None:
                for sp in _children(clause, "export_specifier"):
                    local = sp.child_by_field_name("name")
                    alias = sp.child_by_field_name("alias")
                    if local is not None:
                        add(_str_value(alias or local), kinds.get(_str_value(local), "const"), st)


def _class(node, loc):
    heritage = next(iter(_children(node, "class_heritage")), None)
    ext = None
    if heritage is not None:
        t = _text(heritage)
        ext = t[len("extends"):].strip() if t.startswith("extends") else t
    methods, statics = {}, {}
    body = node.child_by_field_name("body")
    for m in (_children(body, "method_definition", "field_definition") if body is not None else []):
        is_static = any(c.type == "static" for c in m.children)
        if m.type == "method_definition":
            name = m.child_by_field_name("name")
            params = m.child_by_field_name("parameters")
            if name is None:
                continue
            prefix = "static " if is_static else ""
            prefix += next((c.type + " " for c in m.children if c.type in ("get", "set")), "")
            methods[prefix + _text(name)] = _text(params) if params is not None else "()"
        elif is_static:
            prop = m.child_by_field_name("property")
            value = m.child_by_field_name("value")
            if prop is not None and _text(prop) in ("template", "props", "components"):
                statics[_text(prop)] = _text(value) if value is not None else ""
    return {"extends": ext, "methods": methods, "static": statics, "loc": f"{loc}:{_line(node)}"}


def _classes(root, ip, loc, out):
    for st in root.named_children:
        if not _ok(st):
            continue
        decl = st.child_by_field_name("declaration") if st.type == "export_statement" else st
        if decl is not None and decl.type == "class_declaration" and _ok(decl):
            n = decl.child_by_field_name("name")
            is_default = st.type == "export_statement" and any(c.type == "default" for c in st.children)
            name = _text(n) if n is not None else ("default" if is_default else None)
            if name:
                out[f"{ip}::{name}"] = _class(decl, loc)


def _registry_chain(fn):
    """Categories of `registry.category("a").category("b").add`; None if the chain is not a registry."""
    cats, cur = [], fn.child_by_field_name("object")
    while cur is not None and cur.type == "call_expression":
        f = cur.child_by_field_name("function")
        args = cur.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if f is None or f.type != "member_expression" or first is None \
                or _text(f.child_by_field_name("property")) != "category":
            return None
        cats.append(_str_value(first))
        cur = f.child_by_field_name("object")
    if cur is not None and cur.type == "identifier" and _text(cur) == "registry" and cats:
        return "/".join(reversed(cats))
    return None


def _calls(root):
    """All valid call_expression nodes (iterative walk, ERROR nodes ignored)."""
    stack = [root]
    while stack:
        n = stack.pop()
        if not _ok(n):
            continue
        if n.type == "call_expression":
            yield n
        stack.extend(reversed(n.children))


def _object_members(obj):
    methods, props = [], []
    for c in obj.named_children:
        if not _ok(c):
            continue
        if c.type == "method_definition":
            n = c.child_by_field_name("name")
            if n is not None:
                methods.append(_text(n))
        elif c.type == "pair":
            k = c.child_by_field_name("key")
            if k is not None:
                props.append(_str_value(k))
        elif c.type == "shorthand_property_identifier":
            props.append(_text(c))
    return sorted(set(methods)), sorted(set(props))


def _registries_and_patches(root, ip, loc, registries, patches):
    for call in _calls(root):
        fn = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if fn is None or args is None:
            continue
        a = [x for x in args.named_children if _ok(x)]
        if fn.type == "member_expression" and _text(fn.child_by_field_name("property")) == "add" and a:
            cat = _registry_chain(fn)
            if cat is not None:
                value = _text(a[1])[:VALUE_MAX] if len(a) > 1 else ""
                registries[f"{cat}::{_str_value(a[0])}"] = {"value": value, "loc": f"{loc}:{_line(call)}"}
        elif fn.type == "identifier" and _text(fn) == "patch" and len(a) >= 2:
            entry = patches.setdefault(f"{ip}::{_text(a[0])}",
                                       {"methods": [], "properties": [], "loc": f"{loc}:{_line(call)}"})
            if a[-1].type == "object":
                methods, props = _object_members(a[-1])
                entry["methods"] = sorted(set(entry["methods"]) | set(methods))
                entry["properties"] = sorted(set(entry["properties"]) | set(props))
            else:   # patch(Target, mixin()): non-literal extension, keep its text
                entry["extension"] = _text(a[-1])[:VALUE_MAX]


def resolve_source(source, module, rel_file):
    """Absolute import path of an import source: "./x" (relative to static/src/<rel_file>) -> "@module/..."."""
    if not source.startswith("."):
        return source
    path = posixpath.normpath(posixpath.join(posixpath.dirname(rel_file), source))
    if path.endswith("/index"):
        path = path[:-len("/index")]
    return f"@{module}/{path}"


def _imports(root, loc):
    """`import` statements: [{source, imported (name | "default" | "*" | None), local, loc}]."""
    out = []
    for st in root.named_children:
        if st.type != "import_statement" or not _ok(st):
            continue
        src = st.child_by_field_name("source")
        if src is None:
            continue
        base = {"source": _str_value(src), "loc": f"{loc}:{_line(st)}"}
        clause = next(iter(_children(st, "import_clause")), None)
        if clause is None:
            out.append({**base, "imported": None, "local": None})
            continue
        for c in clause.named_children:
            if not _ok(c):
                continue
            if c.type == "identifier":
                out.append({**base, "imported": "default", "local": _text(c)})
            elif c.type == "namespace_import":
                ident = next(iter(_children(c, "identifier")), None)
                out.append({**base, "imported": "*", "local": _text(ident) if ident else None})
            elif c.type == "named_imports":
                for sp in _children(c, "import_specifier"):
                    name = sp.child_by_field_name("name")
                    alias = sp.child_by_field_name("alias")
                    if name is not None:
                        out.append({**base, "imported": _str_value(name),
                                    "local": _str_value(alias or name)})
    return out


def analyze(data, ip, loc, with_imports=False):
    """Structural analysis of a JS file (bytes) -> exports, classes, registries, patches.

    ip: Odoo import path of the file; loc: path shown in the `loc` values (file:line).
    with_imports: adds the "imports" key (import statements, for the impact analysis).
    Never raises: on failure, a warning and empty sections.
    """
    out = {"exports": {}, "classes": {}, "registries": {}, "patches": {}}
    if with_imports:
        out["imports"] = []
    if not tree_sitter_available():
        return out
    try:
        root = _parser().parse(data).root_node
        if with_imports:
            out["imports"] = _imports(root, loc)
        _exports(root, ip, loc, out["exports"])
        _classes(root, ip, loc, out["classes"])
        _registries_and_patches(root, ip, loc, out["registries"], out["patches"])
    except Exception as e:  # error tolerance: one file must never crash a run
        warn(f"cannot analyze JS {loc}: {e}")
        return {"exports": {}, "classes": {}, "registries": {}, "patches": {},
                **({"imports": []} if with_imports else {})}
    return out

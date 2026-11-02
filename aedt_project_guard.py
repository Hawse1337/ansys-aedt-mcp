# -*- coding: utf-8 -*-
"""Hard guard against a PyAEDT footgun that has already destroyed data twice.

The footgun
-----------
    Hfss(project="LensFinal", ...)

When ``project`` is a bare project *name* and that project is not already open,
PyAEDT does not look it up on disk — it creates a **new empty project** with that
name and overwrites the existing ``.aedt`` file. On 2026-10-19 and 2026-10-21 this
wiped ``LensFeed.aedt``, ``LensFeedlz.aedt`` and ``LensFinal.aedt``.

What this module does
---------------------
``install()`` wraps every PyAEDT application class so that, before construction,
a bare project name is rewritten to the absolute path of the existing ``.aedt``.
Passing an absolute path makes PyAEDT *open* the project instead of recreating it
— this reduces that specific risk but does not guarantee protection from all overwrites.

If a bare name matches no file, construction is refused unless
``AEDT_GUARD_ALLOW_CREATE=1``, in which case it is created inside the first
search directory (never in an unspecified location).

To create a project deliberately, call :func:`create_project` with an absolute
path; it refuses to overwrite an existing file.

Escape hatches (environment variables)
--------------------------------------
``AEDT_GUARD_ALLOW_CREATE=1``   allow the old behaviour (bare name creates a project)
``AEDT_GUARD_SEARCH_DIRS``      ``;``-separated extra directories to search

The guard cannot protect explicit paths or guarantee safe concurrent creation.
Unknown bare names fail closed; intentional creation requires explicit authorization.
"""
from __future__ import annotations

import os
import sys

APP_CLASSES = (
    "Hfss",
    "Maxwell3d",
    "Maxwell2d",
    "Icepak",
    "Q3d",
    "Q2d",
    "Circuit",
    "TwinBuilder",
    "Mechanical",
    "Emit",
    "Rmxprt",
    "Hfss3dLayout",
    "MaxwellCircuit",
    "Simplorer",
    "EddyCurrent",
)

DEFAULT_SEARCH_DIRS = (
    r"D:\HFSS Temp",
    r"C:\Users\lingze\Documents\Ansoft",
)

_LOG_PREFIX = "[aedt-guard] "


def _log(message: str) -> None:
    print(_LOG_PREFIX + message, file=sys.stderr, flush=True)


def _search_dirs(dirs=None) -> list[str]:
    candidates: list[str] = []
    if dirs:
        candidates.extend(dirs)
    env_dirs = os.environ.get("AEDT_GUARD_SEARCH_DIRS", "")
    if env_dirs:
        candidates.extend(p for p in env_dirs.split(";") if p.strip())
    candidates.extend(DEFAULT_SEARCH_DIRS)
    candidates.append(os.getcwd())
    seen, out = set(), []
    for d in candidates:
        d = os.path.abspath(os.path.expanduser(d))
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def resolve_project_argument(value, dirs=None):
    """Return a safe ``project`` value for a PyAEDT application constructor.

    * ``None`` / empty            -> returned unchanged
    * absolute path or a path     -> returned unchanged (explicit location wins)
    * bare name that matches a file -> the file's absolute path
    * bare name, no file, ``AEDT_GUARD_ATTACH_OPEN=1`` -> refused: open state is unverified
    * bare name with no match     -> raises, unless AEDT_GUARD_ALLOW_CREATE is set
    """
    if not value or not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return value

    # An explicit location (absolute path, or anything containing a separator)
    # is the caller's deliberate choice: leave it alone.
    if os.path.isabs(text) or os.sep in text or (os.altsep and os.altsep in text):
        return value

    name = text if text.lower().endswith(".aedt") else text + ".aedt"

    for directory in _search_dirs(dirs):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            _log(
                "project %r resolved to existing file %r (opened, not recreated)"
                % (value, candidate)
            )
            return candidate

    if os.environ.get("AEDT_GUARD_ATTACH_OPEN") == "1":
        # A name alone does not prove that an existing project is open in the
        # intended Desktop. Fail closed instead of silently creating a new project.
        raise RuntimeError(
            "aedt-guard refused unverified AEDT_GUARD_ATTACH_OPEN for %r; "
            "discover the exact project path in the explicitly targeted desktop "
            "and pass that path instead." % value
        )

    if os.environ.get("AEDT_GUARD_ALLOW_CREATE") == "1":
        # Creation is authorised, but a bare name would let PyAEDT choose the
        # directory. Pin it to the first search directory so the new .aedt lands
        # somewhere predictable and the caller can see the path in the log.
        target = os.path.join(_search_dirs(dirs)[0], name)
        _log(
            "project %r matches no file; AEDT_GUARD_ALLOW_CREATE=1 so it will be "
            "created at %r" % (value, target)
        )
        return target

    raise RuntimeError(
        "aedt-guard refused to construct a PyAEDT application with project=%r: "
        "no %r found in %s. A bare project name makes PyAEDT create and OVERWRITE "
        "that file. Pass an absolute path (to create a new project) or correct the "
        "name. Set AEDT_GUARD_ALLOW_CREATE=1 only if creating by name is intended."
        % (value, name, _search_dirs(dirs))
    )


def create_project(app_class, path, **kwargs):
    """Create a NEW project at an explicit **absolute** path.

    This is the sanctioned way to create a project. It refuses when the file
    already exists, so a typo can never overwrite an existing design file.

    Parameters
    ----------
    app_class : type
        One of the PyAEDT application classes, e.g. ``ansys.aedt.core.Hfss``.
    path : str
        Absolute path of the .aedt to create. A relative path or a bare name is
        rejected: the caller must say exactly where the file goes.
    **kwargs
        Passed through to the application constructor (``design``, ``port``,
        ``new_desktop``, ...).

    Returns
    -------
    The PyAEDT application instance.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("create_project requires an explicit path")
    raw = os.path.expanduser(path.strip())
    if not os.path.isabs(raw):
        # Reject relative input explicitly: silently anchoring it to the current
        # directory is how people end up with a stray .aedt next to a script.
        raise ValueError("create_project requires an absolute path, got %r" % path)
    target = os.path.abspath(raw)
    if not target.lower().endswith(".aedt"):
        target += ".aedt"
    if os.path.exists(target):
        raise RuntimeError(
            "create_project refused: %r already exists. Use the normal constructor "
            "to open an existing project; never overwrite one." % target
        )
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    kwargs.pop("project", None)
    _log("creating NEW project at %r" % target)
    return app_class(project=target, **kwargs)


def _wrap(cls, name):
    """Return a subclass whose __init__ sanitises the ``project`` argument."""

    class Guarded(cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            if "project" in kwargs:
                kwargs["project"] = resolve_project_argument(kwargs["project"])
            elif args:
                args = (resolve_project_argument(args[0]),) + tuple(args[1:])
            super().__init__(*args, **kwargs)

    Guarded.__name__ = name
    Guarded.__qualname__ = name
    Guarded.__doc__ = getattr(cls, "__doc__", None)
    return Guarded


def install(namespace=None) -> list[str]:
    """Patch PyAEDT application classes. Returns the names actually patched.

    PyAEDT prints a banner to stdout when first imported. The broker speaks a
    one-JSON-line-per-request protocol on stdout, so that banner would corrupt it.
    Everything the import emits is therefore captured and re-emitted on stderr.
    """
    import contextlib
    import io

    captured_out, captured_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(
            captured_err
        ):
            import ansys.aedt.core as aedt_core
    except Exception as exc:  # pragma: no cover - PyAEDT missing
        _log("PyAEDT not importable, guard not installed: %s" % exc)
        return []

    for text in (captured_out.getvalue(), captured_err.getvalue()):
        if text.strip():
            _log("suppressed import output: " + " ".join(text.split())[:400])

    patched = []
    for name in APP_CLASSES:
        original = getattr(aedt_core, name, None)
        if original is None or not isinstance(original, type):
            continue
        if getattr(original, "_aedt_guard_patched", False):
            continue
        guarded = _wrap(original, name)
        guarded._aedt_guard_patched = True
        setattr(aedt_core, name, guarded)
        # keep the submodule attribute in sync for "from ansys.aedt.core.x import Name"
        module = sys.modules.get(getattr(original, "__module__", "") or "")
        if module is not None and getattr(module, name, None) is original:
            setattr(module, name, guarded)
        patched.append(name)

    if namespace is not None and isinstance(namespace, dict):
        for name in patched:
            namespace[name] = getattr(aedt_core, name)

    _log("installed on %d class(es): %s" % (len(patched), ", ".join(patched)))
    return patched


if __name__ == "__main__":  # tiny self-check
    print("resolve('LensFinal') ->", resolve_project_argument("LensFinal"))

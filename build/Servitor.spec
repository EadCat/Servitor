# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for Servitor. Build on the target OS:

    pyinstaller build/Servitor.spec --workpath .pyinstaller --distpath dist
"""

import json
import os
import sys

from PyInstaller.utils.hooks import collect_data_files

# PyInstaller reports success even when an import it could not resolve is
# missing, producing a bundle that dies with ModuleNotFoundError on the user's
# machine. Fail here instead, while the person who can fix it is watching.
_MISSING = []
for _name in ("customtkinter", "paramiko", "pystray", "PIL"):
    try:
        __import__(_name)
    except ImportError:
        _MISSING.append(_name)
if _MISSING:
    raise SystemExit(
        "\nBuild aborted: %s not importable by %s.\n"
        "The bundle would build cleanly and then fail to start.\n"
        "Install the runtime dependencies first:\n"
        "    %s -m pip install -r requirements.txt\n"
        % (", ".join(_MISSING), sys.executable, sys.executable)
    )

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
SRC = os.path.join(ROOT, "src")
ASSETS = os.path.join(ROOT, "assets")

def licence_files():
    """Every bundled package's own licence files, for licenses/<package>/.

    Walked from the packages Servitor imports through what they require on
    this platform, so the set follows requirements.txt without a list to keep.
    A package's licence files carry the texts of whatever it compiles in;
    Pillow's, for instance, those of FreeType, libjpeg and the rest.
    """
    from importlib.metadata import PackageNotFoundError, distribution
    from packaging.requirements import Requirement

    found, seen = [], set()
    todo = ["customtkinter", "paramiko", "pystray", "Pillow"]
    while todo:
        try:
            dist = distribution(todo.pop())
        except PackageNotFoundError:
            continue
        name = dist.metadata["Name"]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        for path in dist.files or []:
            if any(word in path.name.lower() for word in ("licen", "copying", "notice")):
                found.append((str(dist.locate_file(path)), os.path.join("licenses", name)))
        for line in dist.requires or []:
            requirement = Requirement(line)
            # Extras and other platforms' packages (pystray's macOS one) stay out.
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                todo.append(requirement.name)
    # Python's own, wherever this interpreter's build keeps it.
    for path in (os.path.join(sys.base_prefix, "LICENSE.txt"),
                 os.path.join(sys.base_prefix, "lib", "python%d.%d" % sys.version_info[:2],
                              "LICENSE.txt")):
        if os.path.isfile(path):
            found.append((path, os.path.join("licenses", "Python")))
            break
    return found


def conda_licences(binaries, have):
    """The licences of the conda packages whose shared libraries were bundled.

    Only for a build from a conda environment, where Pillow and the rest link
    conda-forge's libjpeg, libtiff and so on rather than compiling them in, so
    their own licence files do not cover them. conda-meta says which package
    each file came from; the package cache keeps its licence texts.
    """
    meta = os.path.join(sys.prefix, "conda-meta")
    if not os.path.isdir(meta):
        return []
    owner = {}
    for entry in os.listdir(meta):
        if entry.endswith(".json"):
            with open(os.path.join(meta, entry)) as handle:
                record = json.load(handle)
            for name in record.get("files", []):
                owner[os.path.join(sys.prefix, name)] = record
    found, done = [], set(have)
    for _dest, source, _kind in binaries:
        record = owner.get(source) or owner.get(os.path.realpath(source))
        if record is None or record["name"].lower() in done:
            continue
        done.add(record["name"].lower())
        texts = os.path.join(record.get("extracted_package_dir", ""), "info", "licenses")
        for root, _dirs, files in os.walk(texts):
            for name in files:
                found.append((os.path.join(root, name), os.path.join(
                    "licenses", record["name"], os.path.relpath(root, texts))))
    return found


# Tk resolves fonts through the host's X and fontconfig stack. Shipping our own
# copies overrides the target machine's font setup, which breaks non-Latin text.
HOST_LIBS = (
    "libX11", "libXft", "libXrender", "libXext", "libXss", "libXau", "libXdmcp",
    "libxcb", "libfontconfig", "libfreetype",
)

LICENCES = licence_files()

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=[],
    # The bundle carries LGPL and Apache components, so its notices travel
    # with it on every platform, not just through the Windows installer.
    datas=[
        (os.path.join(ASSETS, "icon.png"), "assets"),
        # The window icon on Windows, which takes an .ico rather than the .png.
        (os.path.join(ASSETS, "icon.ico"), "assets"),
        # The menu entry that "Servitor --install" writes on Linux.
        (os.path.join(SPECPATH, "servitor.desktop"), "assets"),
        # The OFL copy in here has to travel with the font files it covers.
        (os.path.join(ASSETS, "fonts"), "assets/fonts"),
        (os.path.join(ROOT, "LICENSE"), "."),
        (os.path.join(ROOT, "THIRD-PARTY-NOTICES.md"), "."),
        # Read at run time for the version shown in the window's corner.
        (os.path.join(ROOT, "VERSION"), "."),
    ] + collect_data_files("customtkinter") + LICENCES,
    # PIL.ImageTk finds Tk through this module, imported by name at run time,
    # so the analysis misses it. Without it every CTkImage, the mini sidebar's
    # pin among them, failed in the bundle and stopped that window half built.
    hiddenimports=["PIL._tkinter_finder"],
    # darkdetect._mac_detect reaches for AppKit through ctypes, which PyInstaller
    # cannot follow and warns about on every build. It is macOS-only code and
    # this app is Linux and Windows, so drop it and the warning with it.
    excludes=["test", "tkinter.test", "unittest", "pydoc_data",
              "darkdetect._mac_detect"],
)

if sys.platform.startswith("linux"):
    # Not the private copies a pip wheel vendors into its own <name>.libs folder
    # (Pillow's libxcb and libfreetype among them): their names carry a hash so
    # they never meet the host's, and without them Pillow fails to import.
    a.binaries = [b for b in a.binaries
                  if not os.path.basename(b[0]).startswith(HOST_LIBS)
                  or os.path.dirname(b[0]).endswith(".libs")]

_have = {os.path.basename(dest).lower() for _src, dest in LICENCES}
a.datas += [(os.path.normpath(os.path.join(dest, os.path.basename(src))), src, "DATA")
            for src, dest in conda_licences(a.binaries, _have)]
# CustomTkinter carries the Roboto font (Apache-2.0) without its licence text,
# which Apache-2.0 asks to travel with it; kept in the repository for that.
_roboto = os.path.join(SPECPATH, "licenses", "Roboto", "LICENSE")
a.datas.append((os.path.join("licenses", "Roboto", "LICENSE"), _roboto, "DATA"))

# The GCC runtime ships no licence text of its own in conda-forge's package, and
# PyInstaller may take it from the system; its GPL-3.0 and runtime exception
# texts are kept in the repository and go in whenever it is bundled.
if any(os.path.basename(dest).startswith(("libstdc++", "libgcc_s")) for dest, _s, _k in a.binaries):
    _gcc = os.path.join(SPECPATH, "licenses", "gcc-runtime")
    a.datas += [(os.path.join("licenses", "gcc-runtime", name), os.path.join(_gcc, name), "DATA")
                for name in sorted(os.listdir(_gcc))]

pyz = PYZ(a.pure)

# The icon and version resource are Windows-only; passing them elsewhere just
# makes PyInstaller warn that it is ignoring them.
exe_extras = {}
if sys.platform == "win32":
    exe_extras["icon"] = os.path.join(ASSETS, "icon.ico")
    # Fill the resource template in from VERSION, so the numbers Windows shows
    # cannot drift from the ones the installer stamps. PyInstaller wants a path.
    version = open(os.path.join(ROOT, "VERSION")).read().strip()
    with open(os.path.join(SPECPATH, "version_info.txt")) as handle:
        template = handle.read()
    generated = os.path.join(workpath, "version_info.generated.txt")
    with open(generated, "w") as handle:
        handle.write(template.format(
            version=version, version_tuple=", ".join(version.split(".") + ["0"])))
    exe_extras["version"] = generated

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Servitor",
    console=False,
    **exe_extras,
)

coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Servitor")

"""Export one source/install's definitions with import provenance and no provider access.

Invoke with Python -I so cwd, user site packages and PYTHONPATH cannot substitute
an editable checkout for an installed artifact. Only the source run adds src/.
"""

import argparse
import json
import runpy
import sys
import sysconfig
from pathlib import Path
from unittest.mock import patch


def discover(source: str, exporter: Path, output: Path):
    if source != "-":
        sys.path.insert(0, source)
    expected = (
        (Path(source) if source != "-" else Path(sysconfig.get_path("purelib"))) / "pubship"
    ).resolve()
    sys.argv = [str(exporter), "--output-dir", str(output)]
    with (
        patch("google.auth.default", side_effect=AssertionError("ADC access prohibited")),
        patch("socket.socket.connect", side_effect=AssertionError("Network access prohibited")),
        patch("socket.socket.connect_ex", side_effect=AssertionError("Network access prohibited")),
        patch("socket.getaddrinfo", side_effect=AssertionError("Network access prohibited")),
    ):
        import pubship

        if Path(pubship.__file__).resolve().parent != expected:
            raise RuntimeError(
                "PubShip import did not resolve to the expected source or isolated install"
            )
        runpy.run_path(str(exporter), run_name="__main__")
    modules = {}
    for name, module in tuple(sys.modules.items()):
        if name == "pubship" or name.startswith("pubship."):
            filename = getattr(module, "__file__", None)
            if filename is None or not Path(filename).resolve().is_relative_to(expected):
                raise RuntimeError("PubShip module escaped the expected source or isolated install")
            modules[name] = str(Path(filename).resolve())
    (output / "provenance.json").write_text(
        json.dumps(
            {"version": pubship.__version__, "package_root": str(expected), "modules": modules},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Source src/ path, or - for the isolated installed package")
    parser.add_argument("exporter", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    discover(args.source, args.exporter, args.output)


if __name__ == "__main__":
    main()

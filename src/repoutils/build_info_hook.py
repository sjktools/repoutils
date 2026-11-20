"""A hatchling build hook that records the Git commit a build comes from.

It adds a JSON file, such as ``{"commit": "<sha>", "dirty": false}``, to the
sdist and the wheel. ``dirty`` is ``true`` if any file under the project
differed from the commit, untracked files included, and both are ``null`` if
the project isn't in a Git repository with a commit (or Git isn't installed).
The file holds no timestamp, so building the same commit again gives the same
file. Set where it goes, as a path in the project, with the hook's ``file``
option.

- A wheel built from an unpacked sdist (which has a ``PKG-INFO``), as ``uv
  build`` does, gets the sdist's copy of the file. The hook never runs Git
  there, as the Git repository it would find is not the sdist's.
- An editable install gets no file, as the commit it records would go stale
  with the next one.

repoutils runs this file by path (``[tool.hatch.build.hooks.custom]``), as
building repoutils can't need repoutils. Another project enables it as the
``repoutils-build-info`` hook, with a pinned ``repoutils`` in its
``build-system.requires``::

    [tool.hatch.build.hooks.repoutils-build-info]
    file = "src/example/build_info.json"

Only hatchling imports this module, so repoutils itself still needs nothing
outside the standard library to run.
"""

import json
import shutil
import tempfile
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, override

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin import hookimpl

PLUGIN_NAME = "repoutils-build-info"


def _head_commit() -> Callable[[Path], tuple[str, bool] | None]:
    """``repoutils.checkout.head_commit``, loaded from the file beside this one,
    as hatchling runs this file by path to build repoutils itself, where the
    ``repoutils`` package can't be imported."""
    spec = spec_from_file_location("_repoutils_build_info_checkout", Path(__file__).with_name("checkout.py"))
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    head_commit: Callable[[Path], tuple[str, bool] | None] = module.head_commit
    return head_commit


def build_info(project: Path) -> dict[str, Any]:
    """The file's contents for a build of the project in that directory."""
    head = _head_commit()(project)
    commit, dirty = head if head is not None else (None, None)
    return {"commit": commit, "dirty": dirty}


class BuildInfoHook(BuildHookInterface[Any]):
    PLUGIN_NAME = PLUGIN_NAME

    _temporary: Path | None = None

    @override
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        path = self.config.get("file")
        if not isinstance(path, str) or not path:
            raise ValueError(f"[tool.hatch.build.hooks.{self.PLUGIN_NAME}] file must be the file's path in the project")
        root = Path(self.root)
        if (root / "PKG-INFO").exists():
            source = root / path
            if not source.is_file():
                return
        else:
            self._temporary = Path(tempfile.mkdtemp(prefix="repoutils-build-info-"))
            source = self._temporary / Path(path).name
            source.write_text(json.dumps(build_info(root), indent=2) + "\n")
        target = path if self.target_name == "sdist" else self.build_config.get_distribution_path(path)
        build_data["force_include"][str(source)] = target

    @override
    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if self._temporary is not None:
            shutil.rmtree(self._temporary)
            self._temporary = None


def get_build_hook() -> type[BuildInfoHook]:
    """The hook, for hatchling to find when it runs this file by path."""
    return BuildInfoHook


@hookimpl
def hatch_register_build_hook() -> type[BuildInfoHook]:
    """The hook, for hatchling to find through the ``hatch`` entry point."""
    return BuildInfoHook

"""Resolve a working directory to a project (design section 7)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from aft import gitutil
from aft.config import is_under
from aft.registry import Project, Registry, path_key, split_key, unique_slug

PATH = "path"
REMOTE = "remote"
UNREGISTERED = "unregistered"
NONE = "none"


@dataclass(frozen=True)
class Resolution:
    kind: str
    """How the directory resolved: path, remote, unregistered or none, or recorded when the
    directory is gone and the project is the one its session start recorded."""

    cwd: Path
    """The directory after resolving symlinks and mapping a worktree onto its main checkout."""

    project: Project | None = None
    slug: str | None = None
    root: Path | None = None
    """The project's root on this host: the matched registered path, the repository's main
    checkout, or the directory itself. An unregistered resolution carries it so the
    session-start record can keep it and the dream can register the project from it."""

    alias_added: str | None = None
    """The path key recorded because the project was found by its remote."""

    @property
    def registered(self) -> bool:
        return self.project is not None


def resolve(
    cwd: Path,
    registry: Registry,
    host: str,
    on_alias: Callable[[Project, str], None] | None = None,
    never: Sequence[str] = (),
) -> Resolution:
    """Resolve ``cwd`` to a project.

    1. Resolve symlinks, and map a linked worktree onto its main checkout.
    2. The longest registered ``host:path`` containing the directory wins. Inside a git
       repository, a registered path above the repository does not count, so a nested
       repository or a submodule is never claimed by the repository around it.
    3. Otherwise a project with the same normalized ``origin`` remote wins, and the
       repository's path is recorded as an alias through ``on_alias``.
    4. Otherwise the directory is an unregistered project of its own: the repository's main
       checkout, or the directory itself outside one (DESIGN 7). Its slug is the directory's
       name, prefixed with parent directory names until the registry holds nothing by it, so
       two directories of one name do not share a project. ``root`` names the directory the
       project would be registered at, which the resolution carries to the session-start
       record and the dream registers from. The home directory itself is the project
       ``home``.

    A directory under the system temporary directory, under ``~/Downloads``, or under one of
    ``never`` - the capture exclusions - never becomes a project, whatever it holds: it
    resolves to none and its sessions file as unfiled.
    """
    real = Path(os.path.realpath(cwd))
    info = gitutil.repo_info(real)
    effective = real
    boundary: Path | None = None
    if info is not None:
        effective = info.main / real.relative_to(info.toplevel)
        boundary = info.main

    best: tuple[int, Project, Path] | None = None
    for project in registry.projects:
        for key in project.paths:
            key_host, path = split_key(key)
            if key_host != host or not _within(effective, path):
                continue
            if boundary is not None and not _within(path, boundary):
                continue
            if best is None or len(path.parts) > best[0]:
                best = (len(path.parts), project, path)
    if best is not None:
        _, project, path = best
        return Resolution(PATH, effective, project, project.slug, path)

    if info is not None and info.origin and boundary is not None:
        remote = gitutil.normalize_remote(info.origin)
        for project in registry.projects:
            if remote in project.remotes:
                key = path_key(host, boundary)
                if on_alias is not None:
                    on_alias(project, key)
                return Resolution(REMOTE, effective, project, project.slug, boundary, key)

    # The directory the project would be registered at: a repository is its main checkout,
    # anything else is the directory itself (DESIGN 7 step 4).
    root = boundary if boundary is not None else real
    if _refused(root, never):
        return Resolution(NONE, effective)
    if root == Path.home().resolve():
        # The home directory itself is the project `home` (DESIGN 7).
        return Resolution(UNREGISTERED, effective, None, "home", root)
    return Resolution(UNREGISTERED, effective, None, unique_slug(root, registry), root)


def _refused(root: Path, never: Sequence[str]) -> bool:
    """Whether this directory never becomes a project (DESIGN 7, F49).

    Scratch runs under the system temporary directory, downloads, and anything capture
    excludes keep their sessions unfiled on purpose: a directory that is here today and
    gone tonight must not become a project the registry keeps naming.
    """
    temporary = Path(tempfile.gettempdir()).resolve()
    if root == temporary or temporary in root.parents:
        return True
    downloads = Path.home().resolve() / "Downloads"
    if root == downloads or downloads in root.parents:
        return True
    return is_under(root, tuple(never))


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents

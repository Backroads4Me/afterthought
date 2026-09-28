"""Resolve a working directory to a project (design section 7)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aft import gitutil
from aft.registry import Project, Registry, path_key, slugify, split_key

PATH = "path"
REMOTE = "remote"
UNREGISTERED = "unregistered"
NONE = "none"


@dataclass(frozen=True)
class Resolution:
    kind: str
    """How the directory resolved: path, remote, unregistered or none."""

    cwd: Path
    """The directory after resolving symlinks and mapping a worktree onto its main checkout."""

    project: Project | None = None
    slug: str | None = None
    root: Path | None = None
    """The project's root on this host: the matched registered path, or the repository."""

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
) -> Resolution:
    """Resolve ``cwd`` to a project.

    1. Resolve symlinks, and map a linked worktree onto its main checkout.
    2. The longest registered ``host:path`` containing the directory wins. Inside a git
       repository, a registered path above the repository does not count, so a nested
       repository or a submodule is never claimed by the repository around it.
    3. Otherwise a project with the same normalized ``origin`` remote wins, and the
       repository's path is recorded as an alias through ``on_alias``.
    4. Otherwise a repository is an unregistered project named after its directory, and
       anything else resolves to no project.
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

    if boundary is not None:
        return Resolution(UNREGISTERED, effective, None, slugify(boundary.name), boundary)
    return Resolution(NONE, effective)


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents

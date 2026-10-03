"""The project registry, ``registry.yaml`` in the vault.

A project has a stable id, a slug used for its directories, and the keys that resolve to
it: ``host:path`` entries and normalized remotes. The registry is a cache of discovered
identity, not a gate: an unregistered repository still resolves (see ``keys``).
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from aft.ids import new_id

VERSION = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RESERVED_SLUGS = ("global",)
"""The scope ``global`` reviews the global pages, so no project may take the slug."""


class RegistryError(Exception):
    pass


@dataclass
class Project:
    id: str
    slug: str
    title: str
    paths: list[str] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    created: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "paths": list(self.paths),
            "remotes": list(self.remotes),
            "created": self.created,
        }


def path_key(host: str, path: Path) -> str:
    return f"{host}:{path}"


def split_key(key: str) -> tuple[str, Path]:
    host, sep, path = key.partition(":")
    if not sep or not host or not path.startswith("/"):
        raise RegistryError(f"invalid path key {key!r}; expected host:/absolute/path")
    return host, Path(path)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


class Registry:
    def __init__(self, path: Path, projects: list[Project] | None = None):
        self.path = path
        self.projects: list[Project] = projects or []

    @classmethod
    def load(cls, path: Path) -> Registry:
        if not path.exists():
            return cls(path)
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise RegistryError(f"{path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise RegistryError(f"{path}: expected a mapping with version: {VERSION}")
        entries = data.get("projects") or []
        if not isinstance(entries, list):
            raise RegistryError(f"{path}: projects must be a list")
        projects = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RegistryError(f"{path}: each project must be a mapping")
            try:
                projects.append(
                    Project(
                        id=str(entry["id"]),
                        slug=str(entry["slug"]),
                        title=str(entry.get("title") or entry["slug"]),
                        paths=[str(p) for p in entry.get("paths") or []],
                        remotes=[str(r) for r in entry.get("remotes") or []],
                        created=str(entry.get("created") or ""),
                    )
                )
            except KeyError as exc:
                raise RegistryError(f"{path}: project entry missing {exc}") from exc
        registry = cls(path, projects)
        problems = registry.problems()
        if problems:
            raise RegistryError(f"{path}: {problems[0]}")
        return registry

    def problems(self) -> list[str]:
        found = []
        ids: set[str] = set()
        slugs: set[str] = set()
        keys: dict[str, str] = {}
        for p in self.projects:
            if p.id in ids:
                found.append(f"duplicate project id {p.id}")
            ids.add(p.id)
            if not SLUG_RE.match(p.slug):
                found.append(f"invalid slug {p.slug!r}")
            if p.slug in slugs:
                found.append(f"duplicate slug {p.slug!r}")
            slugs.add(p.slug)
            for key in p.paths:
                try:
                    split_key(key)
                except RegistryError as exc:
                    found.append(str(exc))
                if key in keys:
                    found.append(f"path {key} belongs to both {keys[key]} and {p.slug}")
                keys[key] = p.slug
        return found

    def save(self) -> None:
        data = {"version": VERSION, "projects": [p.to_dict() for p in self.projects]}
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".registry-", suffix=".yaml")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, self.path)

    def by_slug(self, slug: str) -> Project | None:
        return next((p for p in self.projects if p.slug == slug), None)

    def by_id(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def add(self, slug: str, title: str | None = None) -> Project:
        if not SLUG_RE.match(slug):
            raise RegistryError(f"invalid slug {slug!r}: use lowercase letters, digits and -")
        if slug in RESERVED_SLUGS:
            raise RegistryError(f"the slug {slug!r} is reserved for the global pages")
        if self.by_slug(slug):
            raise RegistryError(f"project {slug!r} already exists")
        project = Project(
            id=new_id("p"), slug=slug, title=title or slug, created=date.today().isoformat()
        )
        self.projects.append(project)
        return project

    def add_path(self, project: Project, key: str) -> bool:
        split_key(key)
        for other in self.projects:
            if other is not project and key in other.paths:
                raise RegistryError(f"path {key} already belongs to {other.slug}")
        if key in project.paths:
            return False
        project.paths.append(key)
        return True

    def add_remote(self, project: Project, remote: str) -> bool:
        if remote in project.remotes:
            return False
        project.remotes.append(remote)
        return True

    def rename(self, old: str, new: str) -> Project:
        project = self.by_slug(old)
        if project is None:
            raise RegistryError(f"no project {old!r}")
        if not SLUG_RE.match(new):
            raise RegistryError(f"invalid slug {new!r}: use lowercase letters, digits and -")
        if new in RESERVED_SLUGS:
            raise RegistryError(f"the slug {new!r} is reserved for the global pages")
        if self.by_slug(new):
            raise RegistryError(f"project {new!r} already exists")
        project.slug = new
        return project

    def remove(self, slug: str) -> Project:
        project = self.by_slug(slug)
        if project is None:
            raise RegistryError(f"no project {slug!r}")
        self.projects.remove(project)
        return project

#!/usr/bin/env python3
"""
SCORM Course Editor MVP
Zero third-party dependencies. Python 3.10+ recommended.

Run:
    python server.py
Then open http://127.0.0.1:8765
"""
from __future__ import annotations

import hashlib
import base64
import hmac
import copy
import io
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MEDIA_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm", ".mov", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf"}
JSON_COURSE = "LO/json/course.json"
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB local safety cap
MAX_CHUNK_BYTES = 60 * 1024 * 1024  # stay below common reverse-proxy limits
EDITOR_USERNAME = os.environ.get("EDITOR_USERNAME", "")
EDITOR_PASSWORD = os.environ.get("EDITOR_PASSWORD", "")
CHUNK_UPLOADS: dict[str, dict[str, Any]] = {}
CHUNK_UPLOADS_LOCK = threading.Lock()


def make_closed_temp_file(*, prefix: str, suffix: str = "", directory: Path | None = None) -> Path:
    """Create a temporary file path without leaving an open OS handle.

    tempfile.mkstemp() returns an already-open file descriptor. On Windows, leaving
    that descriptor open prevents this process from reopening/moving/deleting the
    same file and causes WinError 32. Always close it immediately and then reopen
    the path explicitly with a context manager when needed.
    """
    fd, raw_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=str(directory) if directory is not None else None,
    )
    os.close(fd)
    return Path(raw_path)


def clean_zip_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def safe_rel(name: str) -> bool:
    p = PurePosixPath(clean_zip_name(name))
    return ".." not in p.parts and not str(p).startswith("/")


def filesystem_zip_name(name: str) -> str:
    """Recover UTF-8 filenames stored without the ZIP UTF-8 flag.

    Some legacy archives in the supplied course store Arabic path bytes as UTF-8
    but omit the language flag. Python correctly round-trips those raw bytes through
    CP437, which looks like mojibake in the string. Info-ZIP, however, expects the
    actual UTF-8 filesystem spelling when replacing the member.
    """
    name = clean_zip_name(name)
    try:
        raw = name.encode("cp437")
        decoded = raw.decode("utf-8")
        # Only use the recovered spelling when it is a meaningful UTF-8 roundtrip.
        if decoded.encode("utf-8") == raw:
            return decoded
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


def decode_json(raw: bytes) -> dict[str, Any]:
    errors = []
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1256"):
        try:
            value = json.loads(raw.decode(enc))
            if not isinstance(value, dict):
                raise ValueError("JSON root must be an object")
            return value
        except Exception as exc:
            errors.append(f"{enc}: {exc}")
    raise ValueError("Unable to decode JSON: " + " | ".join(errors))


def encode_json(value: dict[str, Any]) -> bytes:
    # Existing packages use UTF-8/BOM in several JSON files; BOM is broadly tolerated
    # and preserves Arabic cleanly for the legacy engine.
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return text.encode("utf-8-sig")


def find_course_paths(names: Iterable[str]) -> list[tuple[str, str]]:
    """Return (course_path, package_prefix) for course.json files."""
    out = []
    marker = JSON_COURSE.lower()
    for original in names:
        name = clean_zip_name(original)
        if name.startswith("__MACOSX/"):
            continue
        low = name.lower()
        if low.endswith(marker):
            prefix = name[: len(name) - len(JSON_COURSE)]
            out.append((name, prefix))
    return out


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_strings(v)


def copy_stream(src, dst, chunk_size=1024 * 1024):
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            break
        dst.write(chunk)


@dataclass
class Package:
    workspace: "Workspace"
    id: str
    display_name: str
    kind: str  # nested | direct
    course_code: str
    course: dict[str, Any]
    internal_prefix: str = ""
    nested_outer_entry: str | None = None
    nested_zip_path: Path | None = None
    direct_outer_prefix: str | None = None
    mirror_outer_prefixes: list[str] = field(default_factory=list)
    modifications: dict[str, Path] = field(default_factory=dict)
    media_names_cache: list[str] | None = None

    def _logical_to_internal(self, rel: str) -> str:
        return clean_zip_name(self.internal_prefix + clean_zip_name(rel))

    def _logical_to_outer(self, rel: str, prefix: str | None = None) -> str:
        base = self.direct_outer_prefix if prefix is None else prefix
        return clean_zip_name((base or "") + clean_zip_name(rel))

    def _mod_path(self, rel: str) -> Path:
        digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()
        return self.workspace.temp_dir / "mods" / self.id / digest

    def read_rel(self, rel: str) -> bytes:
        rel = clean_zip_name(rel)
        if rel in self.modifications:
            return self.modifications[rel].read_bytes()
        if self.kind == "nested":
            assert self.nested_zip_path
            with zipfile.ZipFile(self.nested_zip_path, "r") as z:
                return z.read(self._logical_to_internal(rel))
        assert self.direct_outer_prefix is not None
        with zipfile.ZipFile(self.workspace.source_path, "r") as z:
            return z.read(self._logical_to_outer(rel))

    def write_rel_bytes(self, rel: str, data: bytes):
        rel = clean_zip_name(rel)
        if not safe_rel(rel):
            raise ValueError("Unsafe path")
        p = self._mod_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.modifications[rel] = p
        self.workspace.dirty = True
        if Path(rel).suffix.lower() in MEDIA_EXTS:
            self.media_names_cache = None

    def write_rel_from_stream(self, rel: str, stream, length: int):
        rel = clean_zip_name(rel)
        if not safe_rel(rel):
            raise ValueError("Unsafe path")
        p = self._mod_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        remaining = length
        with p.open("wb") as f:
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Unexpected end of upload")
                f.write(chunk)
                remaining -= len(chunk)
        self.modifications[rel] = p
        self.workspace.dirty = True
        self.media_names_cache = None

    def write_json(self, rel: str, obj: dict[str, Any]):
        self.write_rel_bytes(rel, encode_json(obj))

    def page_list(self) -> list[dict[str, Any]]:
        topics = self.course.get("Topics", {})
        topic = topics.get("Topic", {}) if isinstance(topics, dict) else {}
        pages = topic.get("pages", []) if isinstance(topic, dict) else []
        return pages if isinstance(pages, list) else []

    def topic(self) -> dict[str, Any]:
        topics = self.course.setdefault("Topics", {})
        if not isinstance(topics, dict):
            self.course["Topics"] = topics = {}
        topic = topics.setdefault("Topic", {})
        if not isinstance(topic, dict):
            topics["Topic"] = topic = {}
        return topic

    def save_course(self):
        self.write_json(JSON_COURSE, self.course)

    def model(self) -> dict[str, Any]:
        topic = self.topic()
        pages = []
        for idx, page in enumerate(self.page_list()):
            if not isinstance(page, dict):
                continue
            pages.append({
                "index": idx,
                "name": page.get("name", ""),
                "title": page.get("title", ""),
                "type": page.get("type", ""),
                "template": page.get("template", ""),
            })
        return {
            "id": self.id,
            "name": self.display_name,
            "courseCode": self.course.get("courseCode", self.course_code),
            "courseName": self.course.get("courseName", ""),
            "unitTitle": topic.get("title", ""),
            "scormVersion": self.course.get("scormVersion", ""),
            "pdf": self.course.get("pdf", ""),
            "pages": pages,
            "modifiedCount": len(self.modifications),
            "mirrorCount": len(self.mirror_outer_prefixes),
        }

    def get_page(self, page_name: str) -> dict[str, Any]:
        page_meta = None
        for page in self.page_list():
            if isinstance(page, dict) and page.get("name") == page_name:
                page_meta = page
                break
        if not page_meta:
            raise KeyError("Page not found")
        rel = f"LO/json/{page_name}.json"
        data = decode_json(self.read_rel(rel))
        return {
            "meta": dict(page_meta),
            "data": data,
            "media": self.media_for_page(page_name, data),
        }

    def save_page(self, page_name: str, page_title: str, data: dict[str, Any]):
        found = False
        for page in self.page_list():
            if isinstance(page, dict) and page.get("name") == page_name:
                page["title"] = page_title
                found = True
                break
        if not found:
            raise KeyError("Page not found")
        self.save_course()
        self.write_json(f"LO/json/{page_name}.json", data)

    def save_package_meta(self, payload: dict[str, Any]):
        if "courseName" in payload:
            self.course["courseName"] = str(payload["courseName"])
        if "unitTitle" in payload:
            self.topic()["title"] = str(payload["unitTitle"])
        if "pdf" in payload:
            self.course["pdf"] = str(payload["pdf"])
        self.save_course()

    def all_rel_names(self) -> list[str]:
        if self.media_names_cache is not None:
            return self.media_names_cache
        if self.kind == "nested":
            assert self.nested_zip_path
            with zipfile.ZipFile(self.nested_zip_path, "r") as z:
                names = []
                for name in z.namelist():
                    name = clean_zip_name(name)
                    if self.internal_prefix and name.startswith(self.internal_prefix):
                        names.append(name[len(self.internal_prefix):])
                    elif not self.internal_prefix:
                        names.append(name)
        else:
            assert self.direct_outer_prefix is not None
            with zipfile.ZipFile(self.workspace.source_path, "r") as z:
                names = []
                for name in z.namelist():
                    name = clean_zip_name(name)
                    if name.startswith(self.direct_outer_prefix):
                        names.append(name[len(self.direct_outer_prefix):])
        # Modified names can include replacements/new items.
        names.extend(k for k in self.modifications if k not in names)
        self.media_names_cache = names
        return names

    def media_for_page(self, page_name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        referenced = set()
        for s in iter_strings(data):
            ext = Path(urllib.parse.urlparse(s).path).suffix.lower()
            if ext in MEDIA_EXTS:
                referenced.add(Path(urllib.parse.unquote(s)).name.lower())

        candidates = []
        page_low = page_name.lower()
        for rel in self.all_rel_names():
            ext = Path(rel).suffix.lower()
            if ext not in MEDIA_EXTS:
                continue
            base = Path(rel).name.lower()
            reason = None
            if base in referenced:
                reason = "referenced"
            elif page_low in rel.lower():
                reason = "page-name"
            if reason:
                size = self.file_size(rel)
                candidates.append({"path": rel, "name": Path(rel).name, "extension": ext, "size": size, "reason": reason})
        # referenced first, then filename match
        candidates.sort(key=lambda x: (0 if x["reason"] == "referenced" else 1, x["path"]))
        return candidates[:40]

    def file_size(self, rel: str) -> int:
        if rel in self.modifications:
            return self.modifications[rel].stat().st_size
        target = self._logical_to_internal(rel) if self.kind == "nested" else self._logical_to_outer(rel)
        try:
            if self.kind == "nested":
                assert self.nested_zip_path
                with zipfile.ZipFile(self.nested_zip_path, "r") as z:
                    return z.getinfo(target).file_size
            with zipfile.ZipFile(self.workspace.source_path, "r") as z:
                return z.getinfo(target).file_size
        except Exception:
            return 0

    def media_stream_source(self, rel: str):
        """Return (file-like, length, closer) for media response."""
        rel = clean_zip_name(rel)
        if rel in self.modifications:
            f = self.modifications[rel].open("rb")
            return f, self.modifications[rel].stat().st_size, f.close
        if self.kind == "nested":
            assert self.nested_zip_path
            z = zipfile.ZipFile(self.nested_zip_path, "r")
            internal = self._logical_to_internal(rel)
            info = z.getinfo(internal)
            stream = z.open(info, "r")
            return stream, info.file_size, lambda: (stream.close(), z.close())
        z = zipfile.ZipFile(self.workspace.source_path, "r")
        outer = self._logical_to_outer(rel)
        info = z.getinfo(outer)
        stream = z.open(info, "r")
        return stream, info.file_size, lambda: (stream.close(), z.close())

    def validate(self) -> list[dict[str, str]]:
        issues = []
        try:
            course = decode_json(self.read_rel(JSON_COURSE))
        except Exception as exc:
            return [{"level": "error", "message": f"course.json غير صالح: {exc}"}]
        if not self.has_rel("imsmanifest.xml"):
            issues.append({"level": "error", "message": "imsmanifest.xml مفقود"})
        pages = self.page_list()
        if not pages:
            issues.append({"level": "warning", "message": "لا توجد شاشات معرفة في course.json"})
        for page in pages:
            if not isinstance(page, dict) or not page.get("name"):
                issues.append({"level": "error", "message": "تعريف شاشة غير صالح في course.json"})
                continue
            name = page["name"]
            rel = f"LO/json/{name}.json"
            if not self.has_rel(rel):
                issues.append({"level": "error", "message": f"ملف الشاشة مفقود: {rel}"})
                continue
            try:
                decode_json(self.read_rel(rel))
            except Exception as exc:
                issues.append({"level": "error", "message": f"JSON غير صالح في {name}: {exc}"})
        return issues

    def has_rel(self, rel: str) -> bool:
        rel = clean_zip_name(rel)
        if rel in self.modifications:
            return True
        target = self._logical_to_internal(rel) if self.kind == "nested" else self._logical_to_outer(rel)
        try:
            if self.kind == "nested":
                assert self.nested_zip_path
                with zipfile.ZipFile(self.nested_zip_path, "r") as z:
                    z.getinfo(target)
            else:
                with zipfile.ZipFile(self.workspace.source_path, "r") as z:
                    z.getinfo(target)
            return True
        except KeyError:
            return False


class Workspace:
    def __init__(self, source_path: Path, original_name: str):
        self.id = uuid.uuid4().hex
        self.temp_dir = Path(tempfile.mkdtemp(prefix="scorm-editor-"))
        self.source_path = self.temp_dir / "source.zip"
        shutil.move(str(source_path), self.source_path)
        self.original_name = original_name
        self.packages: dict[str, Package] = {}
        self.dirty = False
        self._scan()

    def close(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _unique_id(self, base: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in base).strip("-") or "package"
        candidate = safe
        i = 2
        while candidate in self.packages:
            candidate = f"{safe}-{i}"
            i += 1
        return candidate

    def _scan_nested_zip(self, outer: zipfile.ZipFile, info: zipfile.ZipInfo, index: int) -> Package | None:
        # Stream nested ZIP to temp; avoids loading 100+ MB packages into RAM.
        nested_path = self.temp_dir / f"nested-{index}.zip"
        with outer.open(info, "r") as src, nested_path.open("wb") as dst:
            copy_stream(src, dst)
        try:
            with zipfile.ZipFile(nested_path, "r") as z:
                course_paths = find_course_paths(z.namelist())
                if not course_paths:
                    nested_path.unlink(missing_ok=True)
                    return None
                course_path, prefix = course_paths[0]
                course = decode_json(z.read(course_path))
        except Exception:
            nested_path.unlink(missing_ok=True)
            return None
        code = str(course.get("courseCode") or Path(info.filename).stem)
        pid = self._unique_id(code)
        return Package(
            workspace=self,
            id=pid,
            display_name=str(course.get("Topics", {}).get("Topic", {}).get("title") or Path(info.filename).stem),
            kind="nested",
            course_code=code,
            course=course,
            internal_prefix=prefix,
            nested_outer_entry=clean_zip_name(info.filename),
            nested_zip_path=nested_path,
        )

    def _scan(self):
        with zipfile.ZipFile(self.source_path, "r") as outer:
            # 1) Nested SCORM ZIPs are preferred as the primary package representation.
            nested_infos = [i for i in outer.infolist() if not i.is_dir() and clean_zip_name(i.filename).lower().endswith(".zip") and not clean_zip_name(i.filename).startswith("__MACOSX/")]
            for idx, info in enumerate(nested_infos):
                pkg = self._scan_nested_zip(outer, info, idx)
                if pkg:
                    self.packages[pkg.id] = pkg

            # 2) Find directly-expanded SCORM packages in the outer ZIP.
            direct_courses = []
            for course_path, prefix in find_course_paths(outer.namelist()):
                try:
                    course = decode_json(outer.read(course_path))
                    direct_courses.append((prefix, course))
                except Exception:
                    continue

            # Match direct copies to nested packages by courseCode; otherwise expose as primary.
            for prefix, course in direct_courses:
                code = str(course.get("courseCode") or "")
                matched = None
                for pkg in self.packages.values():
                    if code and pkg.course_code == code:
                        matched = pkg
                        break
                if matched:
                    if prefix not in matched.mirror_outer_prefixes:
                        matched.mirror_outer_prefixes.append(prefix)
                else:
                    pid = self._unique_id(code or Path(prefix.rstrip("/")).name or "scorm")
                    pkg = Package(
                        workspace=self,
                        id=pid,
                        display_name=str(course.get("Topics", {}).get("Topic", {}).get("title") or code or pid),
                        kind="direct",
                        course_code=code or pid,
                        course=course,
                        direct_outer_prefix=prefix,
                    )
                    self.packages[pid] = pkg

        if not self.packages:
            raise ValueError("لم يتم العثور على حزمة SCORM مدعومة أو LO/json/course.json داخل الملف")

    def summary(self) -> dict[str, Any]:
        return {
            "workspaceId": self.id,
            "originalName": self.original_name,
            "dirty": self.dirty,
            "packages": [p.model() for p in self.packages.values()],
            "packageCount": len(self.packages),
            "pageCount": sum(len(p.page_list()) for p in self.packages.values()),
        }

    def validate(self) -> dict[str, Any]:
        packages = []
        errors = 0
        warnings = 0
        for p in self.packages.values():
            issues = p.validate()
            errors += sum(i["level"] == "error" for i in issues)
            warnings += sum(i["level"] == "warning" for i in issues)
            packages.append({"package": p.id, "name": p.display_name, "issues": issues})
        return {"errors": errors, "warnings": warnings, "packages": packages, "ok": errors == 0}

    def _fast_patch_zip(self, source_zip: Path, output_zip: Path, changes: dict[str, Path]) -> bool:
        """Patch only changed members using the system `zip` utility when available.

        Info-ZIP updates/replaces named members while preserving untouched compressed
        members, which is dramatically faster for 100+ MB media-heavy SCORM packages.
        Returns False when `zip` is unavailable so the portable Python fallback can run.
        """
        zip_cmd = shutil.which("zip")
        if not zip_cmd:
            return False
        shutil.copy2(source_zip, output_zip)
        if not changes:
            return True
        stage = Path(tempfile.mkdtemp(prefix="scorm-patch-", dir=self.temp_dir))
        try:
            top_names = set()
            for member, src in changes.items():
                member = clean_zip_name(member)
                if not safe_rel(member):
                    raise ValueError(f"Unsafe ZIP path: {member}")
                fs_member = filesystem_zip_name(member)
                target = stage.joinpath(*PurePosixPath(fs_member).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                top_names.add(PurePosixPath(fs_member).parts[0])
            cmd = [zip_cmd, "-q", "-r", str(output_zip)] + sorted(top_names)
            subprocess.run(cmd, cwd=stage, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _rebuild_nested(self, pkg: Package) -> Path:
        assert pkg.nested_zip_path
        out = self.temp_dir / f"rebuilt-{pkg.id}.zip"
        mapped_changes = {pkg._logical_to_internal(rel): path for rel, path in pkg.modifications.items()}
        if self._fast_patch_zip(pkg.nested_zip_path, out, mapped_changes):
            return out
        # Portable fallback: rebuild with Python's stdlib. This is slower because
        # unchanged members must be decompressed/recompressed.
        with zipfile.ZipFile(pkg.nested_zip_path, "r") as zin, zipfile.ZipFile(out, "w", allowZip64=True) as zout:
            seen = set()
            for info in zin.infolist():
                name = clean_zip_name(info.filename)
                out_info = copy.copy(info)  # writing mutates ZipInfo.header_offset
                if info.is_dir():
                    zout.writestr(out_info, b"")
                    continue
                if name in mapped_changes:
                    with zout.open(out_info, "w") as dst, mapped_changes[name].open("rb") as src:
                        copy_stream(src, dst)
                    seen.add(name)
                else:
                    # Open the source BEFORE opening the destination so the original
                    # ZipInfo remains valid even if the writer mutates its copy.
                    with zin.open(info, "r") as src, zout.open(out_info, "w") as dst:
                        copy_stream(src, dst)
            # New logical files are unusual but supported.
            for internal_name, path in mapped_changes.items():
                if internal_name in seen or internal_name in zin.namelist():
                    continue
                zout.write(path, internal_name, compress_type=zipfile.ZIP_DEFLATED)
        return out

    def export(self) -> Path:
        validation = self.validate()
        if not validation["ok"]:
            raise ValueError("Cannot export: validation contains errors")

        rebuilt_nested: dict[str, Path] = {}
        for p in self.packages.values():
            if p.kind == "nested" and p.modifications:
                rebuilt_nested[p.id] = self._rebuild_nested(p)

        output = self.temp_dir / (Path(self.original_name).stem + "_edited.zip")
        direct_replacements: dict[str, Path] = {}
        for p in self.packages.values():
            if not p.modifications:
                continue
            prefixes = []
            if p.kind == "direct" and p.direct_outer_prefix is not None:
                prefixes.append(p.direct_outer_prefix)
            prefixes.extend(p.mirror_outer_prefixes)
            for prefix in prefixes:
                for rel, path in p.modifications.items():
                    direct_replacements[clean_zip_name(prefix + rel)] = path

        nested_entry_to_file = {
            clean_zip_name(self.packages[pid].nested_outer_entry or ""): path
            for pid, path in rebuilt_nested.items()
        }

        all_outer_changes = dict(direct_replacements)
        all_outer_changes.update({name: path for name, path in nested_entry_to_file.items() if name})
        if self._fast_patch_zip(self.source_path, output, all_outer_changes):
            return output

        # Portable fallback when the system `zip` utility is unavailable.
        with zipfile.ZipFile(self.source_path, "r") as zin, zipfile.ZipFile(output, "w", allowZip64=True) as zout:
            for info in zin.infolist():
                name = clean_zip_name(info.filename)
                out_info = copy.copy(info)  # writing mutates ZipInfo.header_offset
                if info.is_dir():
                    zout.writestr(out_info, b"")
                    continue
                replacement = all_outer_changes.get(name)
                if replacement:
                    with zout.open(out_info, "w") as dst, replacement.open("rb") as src:
                        copy_stream(src, dst)
                else:
                    with zin.open(info, "r") as src, zout.open(out_info, "w") as dst:
                        copy_stream(src, dst)
        return output


WORKSPACE_LOCK = threading.Lock()
CURRENT: Workspace | None = None


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "SCORMEditor/0.1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _authorized(self) -> bool:
        # Local mode stays password-free. Public launcher sets both env vars.
        if not EDITOR_PASSWORD:
            return True
        expected_user = EDITOR_USERNAME or "editor"
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
            user, password = decoded.split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(user, expected_user) and hmac.compare_digest(password, EDITOR_PASSWORD)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        body = b"Authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SCORM Course Editor"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_json(self, value: Any, status=200):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status=400):
        self._send_json({"error": message}, status)

    def _workspace(self) -> Workspace:
        global CURRENT
        if CURRENT is None:
            raise RuntimeError("No course loaded")
        return CURRENT

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 50 * 1024 * 1024:
            raise ValueError("Invalid request body size")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/state":
                ws = self._workspace()
                return self._send_json(ws.summary())
            if path == "/api/page":
                ws = self._workspace()
                pkg = ws.packages[q.get("package", [""])[0]]
                page = q.get("page", [""])[0]
                return self._send_json(pkg.get_page(page))
            if path == "/api/validate":
                return self._send_json(self._workspace().validate())
            if path == "/api/media":
                ws = self._workspace()
                pkg = ws.packages[q.get("package", [""])[0]]
                rel = q.get("path", [""])[0]
                stream, length, closer = pkg.media_stream_source(rel)
                try:
                    ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    copy_stream(stream, self.wfile)
                finally:
                    closer()
                return
            if path == "/api/export":
                ws = self._workspace()
                out = ws.export()
                size = out.stat().st_size
                filename = out.name
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with out.open("rb") as src:
                    copy_stream(src, self.wfile)
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                return self._serve_static(rel)
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            self.send_error(404)
        except KeyError as exc:
            self._error(f"Not found: {exc}", 404)
        except Exception as exc:
            self._error(str(exc), 500)

    def _serve_static(self, rel: str):
        if not safe_rel(rel):
            return self.send_error(400)
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self.send_error(403)
        if not target.exists() or not target.is_file():
            return self.send_error(404)
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype in ("application/javascript", "application/json") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/upload/start":
                return self._upload_start()
            if path == "/api/upload/chunk":
                return self._upload_chunk(q)
            if path == "/api/upload/finish":
                return self._upload_finish()
            if path == "/api/upload":
                return self._upload(q)
            if path == "/api/replace-media":
                ws = self._workspace()
                pkg = ws.packages[q.get("package", [""])[0]]
                rel = q.get("path", [""])[0]
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2 * 1024 * 1024 * 1024:
                    raise ValueError("Invalid media size")
                pkg.write_rel_from_stream(rel, self.rfile, length)
                return self._send_json({"ok": True, "path": rel, "size": length})
            self.send_error(404)
        except KeyError as exc:
            self._error(f"Not found: {exc}", 404)
        except Exception as exc:
            self._error(str(exc), 500)

    def _upload_start(self):
        payload = self._read_json_body()
        name = str(payload.get("name", "course.zip"))
        size = int(payload.get("size", 0))
        if not name.lower().endswith(".zip"):
            raise ValueError("يرجى رفع ملف ZIP")
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise ValueError("حجم الملف غير صالح أو يتجاوز 4GB")
        upload_id = uuid.uuid4().hex
        tmp = make_closed_temp_file(prefix="scorm-chunk-upload-", suffix=".zip")
        with CHUNK_UPLOADS_LOCK:
            CHUNK_UPLOADS[upload_id] = {"path": tmp, "name": name, "size": size, "received": 0}
        return self._send_json({"uploadId": upload_id, "chunkSize": 50 * 1024 * 1024})

    def _upload_chunk(self, q):
        upload_id = q.get("id", [""])[0]
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_CHUNK_BYTES:
            raise ValueError("حجم جزء الرفع غير صالح")
        with CHUNK_UPLOADS_LOCK:
            info = CHUNK_UPLOADS.get(upload_id)
            if not info:
                raise ValueError("جلسة الرفع غير موجودة أو انتهت")
            expected = int(info["received"])
        offset = int(q.get("offset", [str(expected)])[0])
        if offset != expected:
            raise ValueError(f"ترتيب أجزاء الرفع غير صحيح: expected {expected}, got {offset}")
        remaining = length
        with Path(info["path"]).open("ab") as f:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("انقطع رفع الجزء")
                f.write(chunk)
                remaining -= len(chunk)
        with CHUNK_UPLOADS_LOCK:
            info = CHUNK_UPLOADS.get(upload_id)
            if not info:
                raise ValueError("جلسة الرفع انتهت")
            info["received"] = int(info["received"]) + length
            if info["received"] > info["size"]:
                raise ValueError("تم استقبال بيانات أكبر من حجم الملف المتوقع")
            received = info["received"]
            total = info["size"]
        return self._send_json({"ok": True, "received": received, "size": total})

    def _upload_finish(self):
        global CURRENT
        payload = self._read_json_body()
        upload_id = str(payload.get("uploadId", ""))
        with CHUNK_UPLOADS_LOCK:
            info = CHUNK_UPLOADS.pop(upload_id, None)
        if not info:
            raise ValueError("جلسة الرفع غير موجودة أو انتهت")
        tmp = Path(info["path"])
        try:
            if int(info["received"]) != int(info["size"]):
                raise ValueError(f"الرفع غير مكتمل: {info['received']} من {info['size']} بايت")
            if tmp.stat().st_size != int(info["size"]):
                raise ValueError("حجم الملف المجمع لا يطابق الحجم المتوقع")
            if not zipfile.is_zipfile(tmp):
                raise ValueError("الملف ليس ZIP صالحًا")
            new_ws = Workspace(tmp, str(info["name"]))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        with WORKSPACE_LOCK:
            old = CURRENT
            CURRENT = new_ws
            if old:
                old.close()
        self._send_json(new_ws.summary())

    def _upload(self, q):
        global CURRENT
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            raise ValueError("حجم الملف غير صالح أو يتجاوز 4GB")
        name = urllib.parse.unquote(q.get("name", ["course.zip"])[0])
        if not name.lower().endswith(".zip"):
            raise ValueError("يرجى رفع ملف ZIP")
        tmp = make_closed_temp_file(prefix="scorm-upload-", suffix=".zip")
        try:
            remaining = length
            with tmp.open("wb") as f:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("انقطع رفع الملف")
                    f.write(chunk)
                    remaining -= len(chunk)
            if not zipfile.is_zipfile(tmp):
                raise ValueError("الملف ليس ZIP صالحًا")
            new_ws = Workspace(tmp, name)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        with WORKSPACE_LOCK:
            old = CURRENT
            CURRENT = new_ws
            if old:
                old.close()
        self._send_json(new_ws.summary())

    def do_PUT(self):
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json_body()
            ws = self._workspace()
            if path == "/api/page":
                pkg = ws.packages[str(payload.get("package", ""))]
                page = str(payload.get("page", ""))
                title = str(payload.get("title", ""))
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ValueError("Page data must be an object")
                pkg.save_page(page, title, data)
                return self._send_json({"ok": True, "package": pkg.model()})
            if path == "/api/package":
                pkg = ws.packages[str(payload.get("package", ""))]
                pkg.save_package_meta(payload)
                return self._send_json({"ok": True, "package": pkg.model()})
            self.send_error(404)
        except KeyError as exc:
            self._error(f"Not found: {exc}", 404)
        except Exception as exc:
            self._error(str(exc), 400)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"SCORM Course Editor running on http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        global CURRENT
        if CURRENT:
            CURRENT.close()
            CURRENT = None


if __name__ == "__main__":
    main()

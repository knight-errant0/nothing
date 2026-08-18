r"""Safely clean files from the Windows system Temp directory.

The target is always <Windows directory>\Temp. The script never accepts a
custom path, never follows reparse points, and always requires confirmation.
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
CONFIRMATION_TEXT = "DELETE"


@dataclass(frozen=True)
class Candidate:
    path: Path
    kind: str
    size: int = 0


@dataclass(frozen=True)
class Issue:
    path: Path
    reason: str


def get_system_temp_directory() -> Path:
    """Return the real Windows installation's Temp directory."""
    if os.name != "nt":
        raise RuntimeError("This script can only run on Windows.")

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length == 0:
        raise ctypes.WinError()
    if length >= len(buffer):
        raise RuntimeError("The Windows directory path is unexpectedly long.")

    target = Path(os.path.abspath(Path(buffer.value) / "Temp"))
    validate_normal_directory(target, "System Temp")
    return target


def validate_normal_directory(path: Path, description: str) -> None:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{description} directory does not exist: {path}") from exc

    if not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeError(f"{description} path is not a directory: {path}")
    if is_reparse_point(path_stat):
        raise RuntimeError(f"Refusing to use a reparse point as {description}: {path}")


def is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def is_inside_root(root: Path, path: Path) -> bool:
    root_text = os.path.normcase(os.path.abspath(root))
    path_text = os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath((root_text, path_text)) == root_text
    except ValueError:
        return False


def scan_directory(root: Path) -> tuple[list[Candidate], list[Issue]]:
    candidates: list[Candidate] = []
    issues: list[Issue] = []

    def scan(current: Path) -> None:
        try:
            validate_normal_directory(current, "Scan target")
        except (OSError, RuntimeError) as exc:
            issues.append(Issue(current, str(exc)))
            return

        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            issues.append(Issue(current, format_os_error(exc)))
            return

        for entry in entries:
            path = Path(entry.path)
            if not is_inside_root(root, path) or path == root:
                issues.append(Issue(path, "Path failed the System Temp boundary check."))
                continue

            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                issues.append(Issue(path, format_os_error(exc)))
                continue

            if is_reparse_point(entry_stat):
                issues.append(Issue(path, "Reparse point skipped; its target was not accessed."))
            elif stat.S_ISDIR(entry_stat.st_mode):
                scan(path)
                candidates.append(Candidate(path, "DIR"))
            elif stat.S_ISREG(entry_stat.st_mode):
                candidates.append(Candidate(path, "FILE", entry_stat.st_size))
            else:
                issues.append(Issue(path, "Unsupported file type skipped."))

    scan(root)
    return candidates, issues


def validate_candidate(root: Path, candidate: Candidate) -> None:
    path = candidate.path
    validate_normal_directory(root, "System Temp")
    if path == root or not is_inside_root(root, path):
        raise RuntimeError("Path is outside System Temp.")
    if candidate.kind not in {"FILE", "DIR"}:
        raise RuntimeError("Unsupported candidate type.")

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Path is outside System Temp.") from exc

    current = root
    for part in relative.parts[:-1]:
        current /= part
        parent_stat = os.lstat(current)
        if is_reparse_point(parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
            raise RuntimeError(f"Unsafe parent path detected: {current}")

    current_stat = os.lstat(path)
    if is_reparse_point(current_stat):
        raise RuntimeError("Item became a reparse point after scanning.")
    if candidate.kind == "FILE" and not stat.S_ISREG(current_stat.st_mode):
        raise RuntimeError("Item type changed after scanning.")
    if candidate.kind == "DIR" and not stat.S_ISDIR(current_stat.st_mode):
        raise RuntimeError("Item type changed after scanning.")


def delete_candidates(
    root: Path, candidates: list[Candidate]
) -> tuple[list[Candidate], list[Issue]]:
    deleted: list[Candidate] = []
    failures: list[Issue] = []

    files = [candidate for candidate in candidates if candidate.kind == "FILE"]
    directories = sorted(
        (candidate for candidate in candidates if candidate.kind == "DIR"),
        key=lambda candidate: len(candidate.path.parts),
        reverse=True,
    )

    for candidate in [*files, *directories]:
        try:
            validate_candidate(root, candidate)
            if candidate.kind == "FILE":
                candidate.path.unlink()
            else:
                candidate.path.rmdir()
            deleted.append(candidate)
            print(f"[DELETED] {candidate.path}")
        except FileNotFoundError:
            failures.append(Issue(candidate.path, "Item no longer exists."))
        except OSError as exc:
            failures.append(Issue(candidate.path, format_os_error(exc)))
        except RuntimeError as exc:
            failures.append(Issue(candidate.path, str(exc)))

    return deleted, failures


def format_os_error(error: OSError) -> str:
    return error.strerror or str(error)


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def print_scan_result(
    root: Path, candidates: list[Candidate], issues: list[Issue]
) -> None:
    print(f"System Temp: {root}")
    print("\nItems that will be deleted:")
    if not candidates:
        print("  (none)")
    else:
        for index, candidate in enumerate(candidates, start=1):
            detail = (
                format_size(candidate.size)
                if candidate.kind == "FILE"
                else "remove if empty"
            )
            print(f"{index:>5}. [{candidate.kind}] {candidate.path} ({detail})")

    if issues:
        print("\nItems that will be skipped:")
        for issue in issues:
            print(f"  [SKIP] {issue.path}: {issue.reason}")

    file_count = sum(candidate.kind == "FILE" for candidate in candidates)
    directory_count = sum(candidate.kind == "DIR" for candidate in candidates)
    total_size = sum(candidate.size for candidate in candidates)
    print(
        f"\nSummary: {file_count} file(s), {directory_count} directories, "
        f"{format_size(total_size)}."
    )


def ask_for_confirmation() -> bool:
    try:
        answer = input(
            f"\nType {CONFIRMATION_TEXT} to delete the listed items; "
            "any other input cancels: "
        )
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Nothing was deleted.")
        return False

    if answer.strip() != CONFIRMATION_TEXT:
        print("Cancelled. Nothing was deleted.")
        return False
    return True


def main() -> int:
    try:
        root = get_system_temp_directory()
        candidates, scan_issues = scan_directory(root)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print_scan_result(root, candidates, scan_issues)
    if not candidates:
        print("Nothing to delete.")
        return 0
    if not ask_for_confirmation():
        return 0

    print("\nDeleting confirmed items...")
    deleted, failures = delete_candidates(root, candidates)
    deleted_files = sum(item.kind == "FILE" for item in deleted)
    deleted_directories = sum(item.kind == "DIR" for item in deleted)
    print(
        f"\nFinished: deleted {deleted_files} file(s) and "
        f"{deleted_directories} directories."
    )

    if failures:
        print("Items not deleted:")
        for issue in failures:
            print(f"  [FAILED] {issue.path}: {issue.reason}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

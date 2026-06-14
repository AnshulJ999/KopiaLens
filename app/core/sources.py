"""Snapshot and source helpers, ported from ``kopiatoolkit.py``."""

from __future__ import annotations

import collections
import json

from .kopia import kopia


def all_snapshots(repo: str) -> list[dict]:
    out = kopia(repo, ["snapshot", "list", "--json"]).stdout
    return json.loads(out) if out.strip() else []


def sources_index(repo: str) -> dict[str, list[dict]]:
    by_source: dict[str, list[dict]] = collections.defaultdict(list)
    for snap in all_snapshots(repo):
        src = snap["source"]
        key = f"{src['userName']}@{src['host']}:{src['path']}"
        by_source[key].append(snap)
    for key in by_source:
        by_source[key].sort(key=lambda item: item.get("endTime", ""))
    return dict(by_source)


def is_laptop(key: str) -> bool:
    return "laptop" in key.lower()


def split_sources(srcs: dict[str, list[dict]], mode: str) -> tuple[list[str], list[str]]:
    keys = sorted(srcs)
    non_laptop = [key for key in keys if not is_laptop(key)]
    if mode == "ignore":
        return non_laptop, non_laptop
    if mode == "hide":
        return keys, non_laptop
    return keys, keys


def logical_stats(snaps: list[dict]) -> dict:
    sizes = [snap.get("stats", {}).get("totalSize", 0) for snap in snaps]
    dates = [snap.get("startTime", "")[:10] for snap in snaps if snap.get("startTime")]
    largest_index = max(range(len(sizes)), key=lambda i: sizes[i]) if sizes else 0
    return {
        "count": len(snaps),
        "first": dates[0] if dates else "?",
        "last": dates[-1] if dates else "?",
        "latest": sizes[-1] if sizes else 0,
        "largest": sizes[largest_index] if sizes else 0,
        "largest_date": snaps[largest_index].get("startTime", "")[:10] if snaps else "?",
        "avg": sum(sizes) // len(sizes) if sizes else 0,
        "sum": sum(sizes),
    }


def roots_of(snaps: list[dict], recent: int | None = None) -> list[str]:
    use = snaps[-recent:] if recent else snaps
    return [snap["rootEntry"]["obj"] for snap in use if snap.get("rootEntry", {}).get("obj")]


def resolve_source(repo: str, source: str | None = None, index: int | str | None = None) -> tuple[str, dict[str, list[dict]]]:
    srcs = sources_index(repo)
    keys = sorted(srcs)
    if index is not None:
        return keys[int(index)], srcs
    if source:
        if source in srcs:
            return source, srcs
        hits = [key for key in keys if source.lower() in key.lower()]
        if len(hits) == 1:
            return hits[0], srcs
        raise ValueError(f"'{source}' matched {len(hits)} sources; be exact or use source_index")
    raise ValueError("need source or source_index")


def select_snap(snaps: list[dict], snap: str | None = None) -> dict | None:
    if not snaps:
        return None
    if snap is None or str(snap) in ("last", "latest"):
        return snaps[-1]
    value = str(snap)
    if value in ("first", "baseline"):
        return snaps[0]
    for item in snaps:
        if item["id"] == value:
            return item
    if value.isdigit() and 0 <= int(value) < len(snaps):
        return snaps[int(value)]
    candidates = [item for item in snaps if item.get("startTime", "").startswith(value)]
    return candidates[-1] if candidates else None

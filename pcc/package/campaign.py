"""Generic package test-campaign dashboard helpers."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from pcc.package.schema import CAMPAIGN_PROFILES, campaign_profile

VALID_STATUSES = {"fail", "pass", "selected", "skip", "xfail"}


@dataclass(frozen=True)
class CampaignRecord:
    area: str
    path: str
    status: str
    reason: str = ""
    profile: str = ""
    task: str = ""
    feature: str = ""

    def as_dict(self) -> dict[str, str]:
        data = {
            "area": self.area,
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
        }
        if self.profile:
            data["profile"] = self.profile
        if self.task:
            data["task"] = self.task
        if self.feature:
            data["feature"] = self.feature
        return data


PROFILE_DESCRIPTIONS = {
    name: data["description"] for name, data in CAMPAIGN_PROFILES.items()
}


def select_test_files(root: str | Path, pattern: str = "test_*.py") -> tuple[str, ...]:
    base = Path(root)
    if not base.exists():
        return ()
    return tuple(str(path) for path in sorted(base.rglob(pattern)) if path.is_file())


def _matches_filters(
    path: str, include: tuple[str, ...], exclude: tuple[str, ...]
) -> bool:
    if include and not any(token in path for token in include):
        return False
    if exclude and any(token in path for token in exclude):
        return False
    return True


def _parse_xfail_rule(rule: str) -> tuple[str, str]:
    if "=" in rule:
        token, reason = rule.split("=", 1)
    elif ":" in rule:
        token, reason = rule.split(":", 1)
    else:
        token, reason = rule, "unspecified"
    return token, reason or "unspecified"


def _profile_root(root: str | Path, profile: str) -> Path:
    base = Path(root)
    data = campaign_profile(profile)
    if data is not None:
        nested = base.joinpath(*data["root_parts"])
        if nested.is_dir():
            return nested
    return base


def _profile_metadata(path: str | Path, profile: str) -> tuple[str, str]:
    data = campaign_profile(profile)
    if data is not None:
        return data["files"].get(Path(path).name, ("", ""))
    return "", ""


def _profile_selected(path: str | Path, profile: str) -> bool:
    if not profile:
        return True
    task, _feature = _profile_metadata(path, profile)
    return bool(task)


def _task_counts(records: list[CampaignRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        if record.task:
            counts[record.task] = counts.get(record.task, 0) + 1
    return dict(sorted(counts.items()))


def campaign_selection(
    root: str | Path,
    *,
    pattern: str = "test_*.py",
    area: str = "core",
    include: tuple[str, ...] | list[str] = (),
    exclude: tuple[str, ...] | list[str] = (),
    xfail_rules: tuple[str, ...] | list[str] = (),
    profile: str = "",
) -> dict[str, object]:
    if profile and profile not in PROFILE_DESCRIPTIONS:
        raise ValueError(f"unknown campaign profile {profile!r}")
    include_tuple = tuple(include)
    exclude_tuple = tuple(exclude)
    parsed_rules = tuple(_parse_xfail_rule(rule) for rule in xfail_rules)
    scan_root = _profile_root(root, profile)
    profile_data = campaign_profile(profile)
    effective_area = area
    if profile_data is not None and area == profile_data["default_area"]:
        effective_area = profile_data["area"]
    selected = [
        path
        for path in select_test_files(scan_root, pattern)
        if _profile_selected(path, profile)
        and _matches_filters(path, include_tuple, exclude_tuple)
    ]
    records: list[CampaignRecord] = []
    for path in selected:
        status = "selected"
        reason = ""
        for token, rule_reason in parsed_rules:
            if token and token in path:
                status = "xfail"
                reason = rule_reason
                break
        task, feature = _profile_metadata(path, profile)
        records.append(
            CampaignRecord(
                effective_area,
                path,
                status,
                reason,
                profile=profile,
                task=task,
                feature=feature,
            )
        )
    return {
        "root": str(root),
        "scan_root": str(scan_root),
        "pattern": pattern,
        "area": effective_area,
        "profile": profile,
        "profile_description": PROFILE_DESCRIPTIONS.get(profile, ""),
        "selection_rule": (
            profile_data["selection_rule"]
            if profile_data is not None
            else "pattern/include/exclude"
        ),
        "include": list(include_tuple),
        "exclude": list(exclude_tuple),
        "xfail_rules": [
            {"match": token, "reason": reason} for token, reason in parsed_rules
        ],
        "selected": selected,
        "records": [record.as_dict() for record in records],
        "dashboard": campaign_dashboard(records),
        "task_counts": _task_counts(records),
    }


def campaign_dashboard(
    records: tuple[CampaignRecord, ...] | list[CampaignRecord],
) -> dict[str, object]:
    by_status = {status: 0 for status in sorted(VALID_STATUSES)}
    by_area: dict[str, dict[str, int]] = {}
    xfail_taxonomy: dict[str, int] = {}
    for record in records:
        if record.status not in VALID_STATUSES:
            raise ValueError(f"unknown campaign status {record.status!r}")
        by_status[record.status] += 1
        area_counts = by_area.setdefault(
            record.area, {status: 0 for status in sorted(VALID_STATUSES)}
        )
        area_counts[record.status] += 1
        if record.status == "xfail":
            reason = record.reason or "unspecified"
            xfail_taxonomy[reason] = xfail_taxonomy.get(reason, 0) + 1
    return {
        "total": len(records),
        "by_status": by_status,
        "by_area": by_area,
        "xfail_taxonomy": dict(sorted(xfail_taxonomy.items())),
    }


def compatibility_matrix(targets: dict[str, dict[str, object]]) -> dict[str, object]:
    rows = []
    for name in sorted(targets):
        data = dict(targets[name])
        data["name"] = name
        rows.append(data)
    return {"packages": rows, "count": len(rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package campaign")
    parser.add_argument("--root", required=True)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--area", default="core")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--xfail", action="append", default=[])
    parser.add_argument("--profile", default="")
    parser.add_argument("--out", default=None)
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    report = campaign_selection(
        ns.root,
        pattern=ns.pattern,
        area=ns.area,
        include=ns.include,
        exclude=ns.exclude,
        xfail_rules=ns.xfail,
        profile=ns.profile,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if ns.out:
        out = Path(ns.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

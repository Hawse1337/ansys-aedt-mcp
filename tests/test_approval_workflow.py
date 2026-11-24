# -*- coding: utf-8 -*-
"""Tests for request supersede, atomic schema approval and low-risk auto approval."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from aedt_guard import approval_contract as contract  # noqa: E402
from aedt_guard.builders import sha256_file  # noqa: E402
import approval_apply  # noqa: E402
import approval_inbox  # noqa: E402
import auto_approval  # noqa: E402


def _allowlist(tmp_path: Path, candidate: Path, monkeypatch) -> Path:
    path = tmp_path / "builders.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "builders": {
                    "demo": {
                        "script": str(candidate),
                        "sha256": sha256_file(candidate),
                        "entrypoint": "run",
                        "params": {"design": {"type": "string", "required": True}},
                        "output_roots": [str(tmp_path / "artifacts")],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AEDT_GUARD_BUILDERS", str(path))
    monkeypatch.setenv("AEDT_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AEDT_APPROVAL_REQUESTS_DIR", str(tmp_path / "requests"))
    return path


def _candidate(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_new_request_supersedes_old_pending(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    first = _candidate(
        tmp_path,
        "first.py",
        "def run(design=None):\n    # first attempt\n    return design\n",
    )
    second = _candidate(
        tmp_path,
        "second.py",
        "def run(design=None):\n    # newer\n    return design\n",
    )
    first_record, _ = contract.build_request("demo", str(first), "first attempt")
    second_record, _ = contract.build_request("demo", str(second), "replacement")
    assert first_record["request_id"] != second_record["request_id"]
    assert second_record["superseded_requests"] == [first_record["request_id"]]
    pending = contract.list_requests("pending")
    assert [row["request_id"] for row in pending] == [second_record["request_id"]]
    old, _ = contract.get_request(first_record["request_id"])
    assert old["status"] == "superseded"
    assert old["superseded_by"] == second_record["request_id"]
    try:
        contract.decide_request(first_record["request_id"], "approved")
    except contract.ApprovalRequestError:
        pass
    else:
        raise AssertionError("a superseded request must not be decidable")


def test_new_builder_registration_request_and_apply(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path, "coax.py",
        "def run(design=None, variant=None):\n    return design, variant\n",
    )
    output = tmp_path / "outputs"
    record, _ = contract.build_request(
        "coax_coupon", str(candidate), "first registration",
        request_type="new_builder",
        new_params=[
            {"name": "design", "type": "string", "required": True},
            {"name": "variant", "type": "enum", "values": ["straight", "stepped"], "required": True},
        ],
        output_roots=[str(output)],
        entrypoint="run",
        tested=["py_compile OK"],
    )
    assert record["request_type"] == "new_builder"
    assert record["old_approved_sha256"] == ""
    assert record["new_builder"]["output_roots"] == [str(output.resolve())]
    result = approval_apply.apply_request(
        record, allowlist_path=allowlist, approved_dir=tmp_path / "approved",
        decided_by="test",
    )
    assert result["approved"] is True
    entry = json.loads(allowlist.read_text(encoding="utf-8"))["builders"]["coax_coupon"]
    assert entry["sha256"] == record["candidate_sha256"]
    assert entry["output_roots"] == [str(output.resolve())]
    assert entry["params"]["variant"]["type"] == "enum"
    assert entry["approval"]["request_id"] == record["request_id"]
    assert entry["approval"]["status"] == "approved"


def test_existing_builder_cannot_use_new_builder_type(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path, "another.py", "def run(design=None):\n    return design\n",
    )
    with pytest.raises(contract.ApprovalRequestError, match="already exists"):
        contract.build_request(
            "demo", str(candidate), "bad registration",
            request_type="new_builder", new_params=[],
            output_roots=[str(tmp_path / "outputs")],
        )


def test_repeated_requests_report_churn_warning(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    record = None
    for index in range(3):
        candidate = _candidate(
            tmp_path, "attempt%d.py" % index,
            "def run(design=None):\n    # attempt %d\n    return design\n" % index,
        )
        record, _ = contract.build_request("demo", str(candidate), "iteration %d" % index)
    assert record["approval_churn"]["requests_24h"] >= 3
    assert record["approval_churn"]["level"] == "warning"
    assert "Consolidate" in record["approval_churn"]["message"]


def test_approval_churn_limit_blocks_incremental_requests(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    data["builders"]["demo"]["approval_churn_limit"] = 1
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    first = _candidate(tmp_path, "one.py", "def run(design=None):\n    # one\n    return design\n")
    second = _candidate(tmp_path, "two.py", "def run(design=None):\n    # two\n    return design\n")
    contract.build_request("demo", str(first), "first")
    with pytest.raises(contract.ApprovalRequestError, match="churn limit"):
        contract.build_request("demo", str(second), "second")


def test_already_applied_candidate_is_not_a_new_pending_request(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    record, path = contract.build_request("demo", str(current), "already there")
    assert record["status"] == "approved"
    assert record["already_approved"] is True
    assert path.is_file()
    assert contract.list_requests("pending") == []


def test_atomic_approval_adds_schema_with_hash(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path,
        "candidate.py",
        "def run(design=None, r_rim_mm=92.4):\n    return design, r_rim_mm\n",
    )
    record, _ = contract.build_request("demo", str(candidate), "add aperture", new_params=[{"name": "r_rim_mm", "type": "number", "min": 50, "max": 130}])
    pending = approval_inbox.list_pending()
    assert pending[0]["request_id"] == record["request_id"]
    result = approval_apply.apply_request(
        pending[0],
        allowlist_path=allowlist,
        approved_dir=tmp_path / "approved",
        decided_by="test",
    )
    assert result["approved"] is True
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    entry = data["builders"]["demo"]
    assert entry["sha256"] == sha256_file(candidate)
    assert entry["params"]["r_rim_mm"] == {"type": "number", "min": 50, "max": 130}
    assert contract.list_requests("pending") == []
    archived = json.loads(
        (tmp_path / "requests" / "archive" / (record["request_id"] + ".json")).read_text(encoding="utf-8")
    )
    assert archived["status"] == "approved"
    assert archived["approved_sha256"] == sha256_file(candidate)


def test_low_risk_policy_accepts_small_existing_function_change(tmp_path: Path, monkeypatch):
    current = _candidate(
        tmp_path,
        "builder.py",
        "def run(design=None):\n    return design\n",
    )
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path,
        "safe.py",
        "def run(design=None, r_rim_mm=92.4):\n    # profile\n    return design, r_rim_mm\n",
    )
    entry = json.loads(allowlist.read_text(encoding="utf-8"))["builders"]["demo"]
    request, _ = contract.build_request("demo", str(candidate), "safe change", new_params=[{"name": "r_rim_mm", "type": "number", "min": 50, "max": 130}])
    safe, reasons = auto_approval.low_risk(request, entry)
    assert safe, reasons
    report = auto_approval.auto_approve_pending(
        allowlist_path=allowlist, approved_dir=tmp_path / "approved"
    )
    assert [item["request_id"] for item in report["approved"]] == [request["request_id"]]
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    assert data["builders"]["demo"]["sha256"] == sha256_file(candidate)


def test_low_risk_policy_rejects_new_import(tmp_path: Path, monkeypatch):
    current = _candidate(tmp_path, "builder.py", "def run(design=None):\n    return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(
        tmp_path,
        "unsafe.py",
        "import os\n\ndef run(design=None):\n    return design\n",
    )
    entry = json.loads(allowlist.read_text(encoding="utf-8"))["builders"]["demo"]
    request, _ = contract.build_request("demo", str(candidate), "unsafe change")
    safe, reasons = auto_approval.low_risk(request, entry)
    assert safe is False
    assert any("imports" in reason for reason in reasons)


def test_two_guard_processes_share_one_pending_request(tmp_path, monkeypatch):
    current = _candidate(tmp_path, "current.py", "def run(design=None): return design\n")
    _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(tmp_path, "next.py", current.read_text() + "# next\n")
    command = [sys.executable, "-B", "-c",
               "import sys; from aedt_guard.approval_contract import build_request; "
               "print(build_request('demo',sys.argv[1],'revision')[0]['request_id'])", str(candidate)]
    processes = [subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, text=True) for _ in range(3)]
    ids = [process.communicate(timeout=30)[0].strip() for process in processes]
    assert all(process.returncode == 0 for process in processes)
    assert len(set(ids)) == 1
    assert [row["request_id"] for row in contract.list_requests()] == [ids[0]]


def test_revised_parameter_range_replaces_old_request(tmp_path, monkeypatch):
    current = _candidate(tmp_path, "current.py", "def run(design=None): return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(tmp_path, "next.py", "def run(design=None, radius_mm=5): return radius_mm\n")
    def request(low, high):
        spec = {"name": "radius_mm", "type": "number", "min": low, "max": high}
        return contract.build_request("demo", str(candidate), "revision", new_params=[spec])[0]
    first, second = request(1, 12), request(2, 8)
    assert first["request_id"] != second["request_id"]
    with pytest.raises(approval_apply.ApprovalApplyError, match="pending"):
        approval_apply.apply_request(first, allowlist_path=allowlist, approved_dir=tmp_path/"approved")
    approval_apply.apply_request(second, allowlist_path=allowlist, approved_dir=tmp_path/"approved")
    entry = approval_apply.load_allowlist(allowlist)["builders"]["demo"]
    assert entry["params"]["radius_mm"] == {"type": "number", "min": 2, "max": 8}


def test_failed_allowlist_commit_preserves_approved_bytes(tmp_path, monkeypatch):
    current = _candidate(tmp_path, "current.py", "def run(design=None): return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    def approve(candidate):
        row, _ = contract.build_request("demo", str(candidate), "normal revision")
        return approval_apply.apply_request(row, allowlist_path=allowlist, approved_dir=tmp_path/"approved")
    first = _candidate(tmp_path, "first.py", current.read_text() + "# revision 1\n")
    second = _candidate(tmp_path, "second.py", current.read_text() + "# revision 2\n")
    approve(first)
    baseline = approval_apply.load_allowlist(allowlist)
    def unavailable(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(approval_apply, "save_allowlist", unavailable)
    with pytest.raises(OSError, match="storage unavailable"):
        approve(second)
    assert approval_apply.load_allowlist(allowlist) == baseline
    assert Path(baseline["builders"]["demo"]["script"]).read_bytes() == first.read_bytes()


def test_reconcile_recovers_schema_approval_after_archive_write_interruption(tmp_path, monkeypatch):
    current = _candidate(tmp_path, "current.py", "def run(design=None): return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    candidate = _candidate(tmp_path, "next.py", "def run(design=None, radius_mm=5): return radius_mm\n")
    row, _ = contract.build_request("demo", str(candidate), "dimension",
                  new_params=[{"name": "radius_mm", "type": "number", "min": 2, "max": 8}])
    def unavailable(*args):
        raise OSError("archive unavailable")
    monkeypatch.setattr(approval_apply, "_atomic_json", unavailable)
    with pytest.raises(OSError, match="archive unavailable"):
        approval_apply.apply_request(row, allowlist_path=allowlist, approved_dir=tmp_path/"approved")
    assert contract.reconcile_requests()["already_applied"] == [row["request_id"]]
    assert contract.get_request(row["request_id"])[0]["status"] == "approved"


def test_approve_candidate_from_long_local_path(tmp_path, monkeypatch):
    current = _candidate(tmp_path, "current.py", "def run(design=None): return design\n")
    allowlist = _allowlist(tmp_path, current, monkeypatch)
    nested = tmp_path
    while len(str(nested)) < 275:
        nested = nested / "candidate-source-history"
    try:
        nested.mkdir(parents=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 206:
            pytest.skip("host disallows long paths")
        raise
    candidate = _candidate(nested, "geometry.py", current.read_text() + "# revision\n")
    request, _ = contract.build_request("demo", str(candidate), "long source path")
    result = approval_apply.apply_request(request, allowlist_path=allowlist, approved_dir=tmp_path/"approved")
    assert result["sha256"] == sha256_file(candidate)

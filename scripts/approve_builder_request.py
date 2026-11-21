"""Operator CLI for one invocation of a reviewed AEDT model builder."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aedt_guard.builders import approval_directory, prepare_request, request_id
from aedt_guard.managed import state_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path, help="saved JSON approval_required response")
    args = parser.parse_args(argv)
    response = json.loads(args.response.read_text(encoding="utf-8-sig"))
    if response.get("approval_mode") != "once" or response.get("status") != "approval_required":
        parser.error("this response requires a policy change or a corrected request")
    proposed = response["request"]
    _, request, deviations = prepare_request(proposed["builder_id"], proposed["project_path"],
                                             proposed["outdir"], proposed["params"], proposed["session"])
    digest = request_id(request)
    if not deviations or request != proposed or digest != response.get("request_id"):
        parser.error("request or policy changed; obtain a new guard response")
    print(json.dumps(request, indent=2, ensure_ascii=False))
    print("Outside approved scope:", "; ".join(deviations))
    confirmation = "approve " + digest[:12]
    if not sys.stdin.isatty() or input(f"Type '{confirmation}' to authorize one attempt: ") != confirmation:
        parser.exit(1, "No approval issued.\n")
    root = approval_directory()
    root.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    grant = {"request_id": digest, "nonce": nonce, "ledger_root": str(state_dir().resolve())}
    temp = root / (nonce + ".tmp")
    temp.write_text(json.dumps(grant), encoding="utf-8")
    os.replace(temp, root / (digest + ".json"))
    print("Approved one attempt for", digest)


if __name__ == "__main__":
    main()

"""ledger voucher CLI — `ledvouch doctor` runs the conformance suite (B1).

Exit code IS the verdict: 0 = pass, 1 = fail (machine-verifiable — no human
judgment; see conformance.py). `--json` prints the stable v0 report for
certification pipelines; the default output is the same facts, line per check.
"""

from __future__ import annotations

import argparse
import json
import sys

from .conformance import run_suite

_STATUS_MARK = {"pass": "PASS", "fail": "FAIL", "skip": "skip"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledvouch",
        description="ledger-vouching support CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser(
        "doctor",
        help="run the conformance suite v0 (deployment shape + hermetic "
             "mechanism probes; --live adds real-upstream probes)",
    )
    doctor.add_argument("--json", action="store_true",
                        help="print the machine-readable v0 report")
    doctor.add_argument("--live", action="store_true",
                        help="also probe the real configured upstream "
                             "(requires LEDVOUCH_DOCTOR_MODEL; costs tokens)")
    args = parser.parse_args(argv)

    report = run_suite(live=args.live)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for check in report["checks"]:
            line = f"{_STATUS_MARK[check['status']]:4}  {check['id']} — {check['title']}"
            if check["detail"]:
                line += f" ({check['detail']})"
            print(line)
        print(f"verdict: {report['verdict']}")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

#!/usr/bin/env python3
"""Recover only the four NFHL datasets, preserving all unrelated output bytes."""
import fcntl
import hashlib
import json
import traceback
from copy import deepcopy
from config import DATA
from acquire import LOCK, write_manifest
from arcgis import now
from output import Output
from nfhl_transport import client
from nfhl_fallback import NFHLFallback, LAYERS


def protected_files(selected=None):
    return {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in DATA.iterdir() if p.is_file()
        and not p.name.startswith(tuple(selected or LAYERS))
        and p.name != "manifest.json"
        and not p.name.startswith(("log", "requests", "manifest.files"))
    }


def refresh(selected=None):
    selected = list(LAYERS) if selected is None else list(selected)
    if not selected or any(name not in LAYERS for name in selected):
        raise ValueError("Refresh selection must contain only the four named NFHL datasets")
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An acquisition is already running; NFHL-only refresh was not started")
        manifest = json.loads((DATA / "manifest.json").read_text())
        before = protected_files(selected)
        unrelated_entries = deepcopy([e for e in manifest["datasets"] if e["id"] not in selected])
        out = Output(append=True)
        transport = client(out)
        fallback = NFHLFallback(transport, out)
        run_id = now().replace(":", "").replace(".", "").replace("+", "_")
        run = dict(started_at=now(), scope=selected, status="running")
        manifest.setdefault("nfhl_refresh_history", []).append(run)
        manifest["status"] = "running"
        out.log("NFHL-ONLY REFRESH: preserving all non-NFHL files and previous request ledgers.")
        write_manifest(out, manifest, transport)
        try:
            for name in selected:
                result = fallback.recover(name)
                old = next((e for e in manifest["datasets"] if e["id"] == name), None)
                result["last_refresh_status"] = result["status"]
                if result["status"] == "failed" and old and old.get("status") == "success":
                    # A failed refresh must not destroy previously acquired true data.
                    retained = deepcopy(old)
                    retained.update(last_refresh_status="failed", retained_previous_output=True,
                                    last_refresh_attempt=dict(timestamp=now(), error=result["error"],
                                                              attempts=result["attempts"],
                                                              all_fallback_stages_exhausted=True))
                    result = retained
                if old is not None:
                    manifest["datasets"][manifest["datasets"].index(old)] = result
                else:
                    manifest["datasets"].append(result)
                print(f"{name}: {result['status']} (refresh={result['last_refresh_status']})", flush=True)
                write_manifest(out, manifest, transport)
            run.update(status="finished", finished_at=now())
        except Exception:
            run.update(status="failed", finished_at=now(), error=traceback.format_exc()[:2000])
            out.log(traceback.format_exc())
            raise
        finally:
            fallback.county.close()
            ledger = out.json_parts(f"requests.nfhl-{run_id}", transport.requests, "requests")
            previous = manifest.setdefault("request_ledger", [])
            # Preserve discovery records made while checking the MSC form contract.
            discovery = [p.name for p in DATA.glob("requests.nfhl-discovery.part*.json")]
            for name in discovery + [f["name"] for f in ledger]:
                if name not in previous:
                    previous.append(name)
            run["request_ledger"] = [f["name"] for f in ledger]
            run["protected_non_nfhl_files"] = len(before)
            run["unrelated_files_unchanged"] = before == protected_files(selected)
            run["unrelated_manifest_entries_unchanged"] = unrelated_entries == [
                e for e in manifest["datasets"] if e["id"] not in selected]
            failures = sum(e.get("status") == "failed" or e.get("last_refresh_status") == "failed"
                           for e in manifest["datasets"])
            manifest.update(status="partial" if failures or run["status"] == "failed" else "complete",
                            failed_datasets=failures, finished_at=now())
            out.log(f"NFHL-ONLY REFRESH FINISHED: {failures} unresolved dataset failures; "
                    f"non-NFHL files unchanged={run['unrelated_files_unchanged']}")
            write_manifest(out, manifest, transport)
            if not run["unrelated_files_unchanged"] or not run["unrelated_manifest_entries_unchanged"]:
                raise AssertionError("Non-NFHL preservation check failed")
        return 0


if __name__ == "__main__":
    raise SystemExit(refresh())

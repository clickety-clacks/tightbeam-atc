"""Opt-in real gateway interoperability; every run owns a new scratch base."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import FeedCursor
from atc_upstream import Gateway, PackagedCLI


@unittest.skipUnless(os.environ.get("ATC_INTEROP_PACKAGE"),
                     "requires an explicit gateway package for scratch interoperability")
class OperatorInteropTest(unittest.TestCase):
    def test_operator_collections_and_org_wide_feed(self):
        from websockets.sync.client import connect

        package = Path(os.environ["ATC_INTEROP_PACKAGE"]).resolve()
        expected_sha = os.environ["ATC_INTEROP_GATEWAY_SHA"]
        expected_build = int(os.environ["ATC_INTEROP_GATEWAY_BUILD"])
        expected_version = os.environ["ATC_INTEROP_GATEWAY_VERSION"]
        scratch = tempfile.TemporaryDirectory(prefix="atc-interop-")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        home, base = root / "home", root / "gateway"
        home.mkdir()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        # Never inherit an operator's endpoint, base, credentials or release node.
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
               "TIGHTBEAM_BASE_DIR": str(base), "TIGHTBEAM_PORT": str(port),
               "TIGHTBEAM_ADVERTISED_URL": f"ws://127.0.0.1:{port}",
               "TIGHTBEAM_LOCAL_HOST_NAME": "scratch", "ERL_FLAGS": "+S 2:2",
               "GIT_AUTHOR_NAME": "Scratch", "GIT_AUTHOR_EMAIL": "scratch@example.invalid",
               "GIT_COMMITTER_NAME": "Scratch", "GIT_COMMITTER_EMAIL": "scratch@example.invalid"}
        # The private boot log is deleted with the scratch base, never exported.
        log = (root / "boot.log").open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen([str(package / "bin/tightbeam-gateway")],
                                   cwd=root, env=env, stdout=log, stderr=log)
        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.addCleanup(stop)
        cli_path = str(package / "bin/tightbeam")
        user = "scratch-reader"
        gateway = Gateway(f"http://127.0.0.1:{port}", PackagedCLI(cli_path, str(base), user))
        deadline = time.monotonic() + 60
        version = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"scratch gateway exited before readiness: {process.returncode}")
            try:
                version = gateway.version()
                break
            except Exception:
                time.sleep(0.1)
        self.assertIsNotNone(version, "scratch gateway did not become ready")
        self.assertEqual(expected_version, version["version"])
        self.assertEqual(expected_build, version["build"])
        self.assertTrue(isinstance(version["sha"], str) and
                        expected_sha.startswith(version["sha"]) and len(version["sha"]) >= 7,
                        "runtime gateway source stamp differs from the requested source")

        def command(*args, bootstrap=False):
            argv = [cli_path, *args]
            if not bootstrap:
                argv += ["--as-user", user]
            result = subprocess.run(argv, cwd=root, env=env, capture_output=True,
                                    text=True, timeout=30)
            # Do not include command output in failures: it can contain credentials.
            self.assertEqual(0, result.returncode, f"scratch command failed: {args[0]}")
            try:
                value = json.loads(result.stdout)
            except ValueError:
                self.fail(f"scratch command returned non-JSON: {args[0]}")
            self.assertIsInstance(value, dict)
            self.assertFalse(bool(value.get("error")), f"scratch command refused: {args[0]}")
            return value

        command("add-user", user, bootstrap=True)
        initial = gateway.read("work-items")
        self.assertTrue(initial.complete, "initial work-item inventory incomplete")
        self.assertEqual([], initial.rows)
        counts = {}
        for resource in ("sessions", "decision-requests", "assignments", "roles", "artifacts"):
            read = gateway.read(resource)
            self.assertTrue(read.complete, f"incomplete {resource} inventory")
            counts[resource] = len(read.rows)

        with connect(f"ws://127.0.0.1:{port}/ws/changes?protocolVersion=2",
                     proxy=None, ping_interval=None, max_size=None, open_timeout=10) as stream:
            # Use the reviewed adapter's runtime credential consumer, not a token fixture.
            gateway.authenticate_feed(stream)
            auth = json.loads(stream.recv(timeout=10))
            self.assertEqual("auth_result", auth.get("type"))
            self.assertIs(auth.get("success"), True)
            self.assertEqual(user, auth.get("userId"))
            stream.send(json.dumps({"type": "subscribe", "subscriptionId": "interop",
                                    "protocolVersion": 2, "filters": {}}))
            ready = json.loads(stream.recv(timeout=10))
            self.assertEqual("subscription_ready", ready.get("type"))
            cursor = FeedCursor(time.monotonic())
            identifiers = set()
            for title in ("Scratch inventory one", "Scratch inventory two"):
                row = command("work-item-create", "--title", title)
                identifiers.add(row["id"])
            observed = set()
            deadline = time.monotonic() + 15
            while observed != identifiers and time.monotonic() < deadline:
                event = json.loads(stream.recv(timeout=max(0.1, deadline - time.monotonic())))
                cursor.accept(event, time.monotonic())
                if event.get("type") == "change" and event.get("resource") == "work-items":
                    key = event.get("refs", {}).get("workItemId")
                    if key in identifiers:
                        self.assertEqual("upsert", event.get("op"))
                        observed.add(key)
            self.assertEqual(identifiers, observed, "org-wide stream missed seeded work items")
        final = gateway.read("work-items")
        self.assertTrue(final.complete)
        self.assertEqual(identifiers, {row["id"] for row in final.rows})
        for key in identifiers:
            detail = gateway.detail("work-items", key)
            self.assertTrue(detail.complete)
            self.assertEqual(key, detail.rows[0]["id"])
        # Emit only nonsecret version/count evidence, never auth frames or raw responses.
        print("ATC_INTEROP_EVIDENCE=" + json.dumps({
            "gateway": version, "gatewaySource": expected_sha,
            "collectionsComplete": True, "initialCounts": counts,
            "workItemsRead": len(final.rows), "workItemDetailsRead": len(identifiers),
            "protocolVersion": 2, "orgWideSubscription": True,
            "matchingEvents": len(observed), "deviceTokenUsed": False,
            "sessionHistoriesExercised": False,
        }, sort_keys=True), flush=True)

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_git import GitResolver


class ProofThroughputTest(unittest.TestCase):
    def test_positive_item_stops_unnecessary_branches_and_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = GitResolver(tmp)
            self.addCleanup(resolver.acquisitions.close)
            refs = [{"repo": "clickety-clacks/example", "commit": f"{n:040x}"}
                    for n in range(100)]
            with (patch.object(resolver, "_origin", return_value="remote"),
                  patch.object(resolver, "_target_refs", return_value=(["main", "0.4"], True)),
                  patch.object(resolver, "_probe", return_value=True) as probe):
                self.assertIs(True, resolver.resolve(refs))
                self.assertEqual(1, probe.call_count)
            self.assertEqual([], resolver.diagnostic_summary()["refs"])

    def test_registered_search_continues_until_proof_then_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = GitResolver(tmp, refs={"first": [], "second": [], "third": []})
            self.addCleanup(resolver.acquisitions.close)
            with (patch.object(resolver.acquisitions, "get", return_value={"refs": ["main"], "complete": True}),
                  patch.object(resolver, "_probe", side_effect=[None, True]) as probe):
                self.assertIs(True, resolver._registered_ancestry("a" * 40))
                self.assertEqual(["first", "second"], [call.args[0] for call in probe.call_args_list])

    def test_negative_and_unknown_still_examine_all_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = GitResolver(tmp)
            self.addCleanup(resolver.acquisitions.close)
            refs = [{"repo": "clickety-clacks/example", "commit": f"{n:040x}"} for n in range(3)]
            with (patch.object(resolver, "_origin", return_value="remote"),
                  patch.object(resolver, "_target_refs", return_value=(["main"], True)),
                  patch.object(resolver, "_probe", side_effect=[False, None, False]) as probe):
                self.assertIsNone(resolver.resolve(refs))
                self.assertEqual(3, probe.call_count)

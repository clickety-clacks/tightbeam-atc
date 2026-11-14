"""Preserve complete parity evidence before an assertion can stop a test."""
import json
import os
from pathlib import Path

from test_assignment_source_failure import publication


def differences(left, right, path=()):
    """Include value and presence differences without truncating nested values."""
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(left.keys() | right.keys()):
            if key not in left or key not in right:
                result.append(dict(path=list(path + (key,)),
                    freshPresent=key in left, warmPresent=key in right,
                    fresh=left.get(key), warm=right.get(key)))
            else:
                result.extend(differences(left[key], right[key], path + (key,)))
        return result
    if isinstance(left, list) and isinstance(right, list):
        result = []
        for index in range(max(len(left), len(right))):
            if index >= len(left) or index >= len(right):
                result.append(dict(path=list(path + (index,)),
                    freshPresent=index < len(left), warmPresent=index < len(right),
                    fresh=left[index] if index < len(left) else None,
                    warm=right[index] if index < len(right) else None))
            else:
                result.extend(differences(left[index], right[index], path + (index,)))
        return result
    if type(left) is not type(right) or left != right:
        return [dict(path=list(path), freshPresent=True, warmPresent=True,
                     fresh=left, warm=right)]
    return []


def assert_publication_equal(case, fresh, warm, label):
    normalized_fresh, normalized_warm = publication(fresh), publication(warm)
    directory = os.environ.get("ATC_PARITY_EVIDENCE")
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        (output / (label + ".json")).write_text(json.dumps(dict(
            fresh=fresh, warm=warm,
            rawDifferences=differences(fresh, warm),
            nonvolatileDifferences=differences(normalized_fresh, normalized_warm),
        ), indent=2) + "\n")
    case.assertEqual(normalized_fresh, normalized_warm)

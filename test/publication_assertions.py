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


def capture_publication(fresh, warm, label, **context):
    normalized_fresh, normalized_warm = publication(fresh), publication(warm)
    directory = os.environ.get("ATC_PARITY_EVIDENCE")
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        (output / (label + ".json")).write_text(json.dumps(dict(
            fresh=fresh, warm=warm,
            rawDifferences=differences(fresh, warm),
            nonvolatileDifferences=differences(normalized_fresh, normalized_warm),
            **context,
        ), indent=2) + "\n")
    return normalized_fresh, normalized_warm


def assert_publication_equal(case, fresh, warm, label):
    normalized_fresh, normalized_warm = capture_publication(fresh, warm, label)
    case.assertEqual(normalized_fresh, normalized_warm)


def assert_winner_exit_publication(case, fresh, warm, label, affected):
    """R19 permits only the named session's retained observation/evidence."""
    normalized_fresh, normalized_warm = capture_publication(fresh, warm, label,
        winnerExitSessions=sorted(affected))
    fresh_agents = {agent["sessionKey"]: agent for agent in fresh["agents"]}
    warm_agents = {agent["sessionKey"]: agent for agent in warm["agents"]}
    for session in affected:
        cold, retained = fresh_agents[session], warm_agents[session]
        case.assertIsNotNone(retained["idleSince"])
        if cold["idleSince"] is not None:
            case.assertGreaterEqual(retained["idleSince"], cold["idleSince"])
        facts = retained["evidence"]["idleFor"]
        case.assertEqual("unconfirmed", facts["freshness"])
        case.assertFalse(facts["complete"])
        case.assertIsNotNone(facts["unconfirmedSince"])
    unexpected = []
    for change in differences(normalized_fresh, normalized_warm):
        path = change["path"]
        allowed = False
        if len(path) >= 3 and path[0] == "agents" and isinstance(path[1], int):
            index = path[1]
            left, right = normalized_fresh["agents"][index], normalized_warm["agents"][index]
            allowed = (left["sessionKey"] == right["sessionKey"] in affected and
                       (path[2:] == ["idleSince"] or path[2:4] == ["evidence", "idleFor"]))
        if not allowed:
            unexpected.append(change)
    case.assertEqual([], unexpected)

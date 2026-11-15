"""Preserve complete parity evidence before an assertion can stop a test."""
from copy import deepcopy
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


def held_scope_metadata(case, fresh, warm, label):
    """R34: explain only eight fields from actual held scope identities.

    Read each server's retained scopes and confirmation inputs separately.
    Neither a hold count nor observed metadata supplies an arbitrary offset.
    Return permitted values only after verifying their exact derivation.
    """
    held = {('attests', aid) for aid, hold in warm.assignment_holds.assignments.items()
            if hold.conflict or hold.incomplete_contribution}
    def inputs(service):
        groups = {'historyScopes': set(service._history_scopes()),
                  'active': set(service.backstop.active),
                  'history': set(service._repeated_history_scopes())}
        return {name: {scope: service.model.inventories[scope].confirmed_at
                       for scope in scopes} for name, scopes in groups.items()}
    cold, retained = inputs(fresh), inputs(warm)
    capture_publication(fresh.current, warm.current, label + '-scope-evidence',
        heldAssignments=sorted(aid for _, aid in held),
        scopeAccounting={side: {name: [dict(scope=list(scope), confirmedAt=at)
                                      for scope, at in sorted(scopes.items())]
                                for name, scopes in groups.items()}
                         for side, groups in (('fresh', cold), ('warm', retained))},
        sweepInputs={side: dict(scopeBudget=service.scope_budget,
                               rebuildInterval=service.rebuild_interval)
                     for side, service in (('fresh', fresh), ('warm', warm))})
    actual_cold = fresh.current['freshness']['reconciliation']
    actual_warm = warm.current['freshness']['reconciliation']
    permitted, deltas = {}, {}
    for name in cold:
        left, right = cold[name], retained[name]
        case.assertEqual(set(left) - held, set(right) - held, name)
        if name == 'historyScopes':
            fields = {'historyScopes': (len(left), len(right))}
        else:
            case.assertEqual({s: at for s, at in left.items() if s not in held},
                             {s: at for s, at in right.items() if s not in held}, name)
            def oldest(scopes):
                return min((at for at in scopes.values() if at is not None), default=None)
            fields = {name + '.scopeCount': (len(left), len(right)),
                      name + '.unconfirmedScopes': (sum(at is None for at in left.values()),
                                                    sum(at is None for at in right.values())),
                      name + '.oldestConfirmedAt': (oldest(left), oldest(right))}
        for path, (expected_left, expected_right) in fields.items():
            bits = path.split('.')
            def value(metadata):
                return metadata[bits[0]][bits[1]] if len(bits) == 2 else metadata[bits[0]]
            case.assertEqual(expected_left, value(actual_cold), 'fresh ' + path)
            case.assertEqual(expected_right, value(actual_warm), 'warm ' + path)
            if not path.endswith('oldestConfirmedAt'):
                def contribution(scopes):
                    return sum(s in held and (not path.endswith('unconfirmedScopes') or at is None)
                               for s, at in scopes.items())
                delta = expected_right - expected_left
                case.assertEqual(contribution(right) - contribution(left), delta, path)
                case.assertLessEqual(abs(delta), len(held), path)
                deltas[path] = delta
            permitted[path] = expected_right
    case.assertEqual(fresh.scope_budget, warm.scope_budget)
    case.assertEqual(fresh.rebuild_interval, warm.rebuild_interval)
    for service, groups, metadata in ((fresh, cold, actual_cold), (warm, retained, actual_warm)):
        count = len(groups['history'])
        estimate = ((count + service.scope_budget - 1) // service.scope_budget
                    * service.rebuild_interval * 1000)
        case.assertEqual(estimate, metadata['estimatedSweepMs'])
    permitted['estimatedSweepMs'] = actual_warm['estimatedSweepMs']
    return permitted, deltas


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


def assert_replay_current(case, service, baseline, label):
    """Apply actual retained SSE frames and compare the complete current frame."""
    full = json.loads(service.event.split(b"data: ", 1)[1])
    case.assertEqual(service.current, full)
    result = service.next_event(baseline["revision"], timeout=0)
    if baseline["revision"] == service.revision:
        case.assertIsNone(result)
        capture_publication(full, baseline, label,
                            replayBaseline=baseline, replayFrames=[])
        case.assertEqual(full, baseline)
        return
    case.assertIsNotNone(result)
    revision, wire = result
    frames = []
    reconstructed = deepcopy(baseline)
    for encoded in (part for part in wire.split(b"\n\n") if part):
        payload = json.loads(encoded.split(b"data: ", 1)[1])
        frames.append(payload)
        if encoded.startswith(b"event: model"):
            reconstructed = payload
            continue
        case.assertEqual(reconstructed["revision"], payload["baseRevision"])
        for name, change in payload["changes"].items():
            rows = {row["id"]: row for row in reconstructed[name]}
            for key in change["remove"]:
                rows.pop(key, None)
            for row in change["upsert"]:
                rows[row["id"]] = row
            reconstructed[name] = list(rows.values())
        reconstructed.update({key: value for key, value in payload.items()
                              if key not in {"baseRevision", "changes"}})
    capture_publication(full, reconstructed, label,
                        replayBaseline=baseline, replayFrames=frames)
    case.assertEqual(service.revision, revision)
    case.assertEqual(full, reconstructed)

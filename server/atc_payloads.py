"""Payload policies established from the last audited published source.

The audited build is diagnostic only: it no longer gates whether feed payloads
are considered. Each payload still has to satisfy its resource's field and
version checks; uncertain payloads use targeted reads. See
docs/payload-guarantees.md for source coverage and remaining runtime evidence.
"""

SOURCE_SHA = "8433d22fdf86027d62a34e9923501775225813ad"
BUILD_ID = ("0.1.9", 2908, "8433d22f")
WORK_ITEM_SUMMARY_SOURCE_SHA = "b09a64a939ca00a86033fd5294b6a8dc6968df5d"
WORK_ITEM_SUMMARY_BUILD_ID = ("0.1.15", 3866, "b09a64a9")
PAYLOAD_POLICIES = {
    "work-items": "strict-version", "sessions": "strict-version",
    "attests": "immutable", "decision requests": "strict-version",
    "artifacts": "strict-version",
}

WORK_ITEM_FIELDS = frozenset("""
    id title specRefName specRefSha256 isBug ownerUserId state failReason routingWakeId
    slateWakeId createdByUser createdBySession createdInTurnSeq createdContextKnown
    createdAt priority rowVersion
""".split())
WORK_ITEM_SUMMARY_FIELDS = frozenset("""
    holders started attestCounts verdictCounts effectiveCommitRefs landings
""".split())
SESSION_FIELDS = frozenset("""
    sessionKey displayName kind orderIndex isBuiltIn adopted ownerUserId origin spawnedBy
    topologyParent handle archetype overrides identityName identityRevision harness provider
    model thinkingLevel modelContext host clearedThroughSeq state createdAt updatedAt
    mechanicalStatus capabilities rowVersion
""".split())
DECISION_FIELDS = frozenset("""
    id kind raiserId raiserSessionKey ownerUserId assignmentId expecterSessionKey expecterUserId
    lineageRung effortGeneration deadlineWakeId raisedAt deadlineAt statuteName question options
    context status decision rationale ruledBy ruledAt consumedAt withdrawnBy withdrawnReason
    withdrawnAt askedOfRole answer answeredBy answeredAt rowVersion
""".split())
ARTIFACT_FIELDS = frozenset("""
    artifactId kind title description createdBySession workItemId parentSession originPath
    contentSha256 recordedMessageId recordedTurnEvidence state home createdAt updatedAt rowVersion
""".split())

CLASSES = {
    "work-items": {"work_item." + name for name in
                   ("created", "updated", "iceboxed", "reopened", "closed", "failed")},
    "sessions": {"session." + name for name in
                 ("spawned", "updated", "harness_changed", "retired")},
    "attests": {"attest.filed"},
    "decision requests": {"decision_request." + name for name in
                          ("opened", "updated", "answered", "returned", "ruled", "consumed",
                           "withdrawn", "superseded")},
    "artifacts": {"artifact.recorded", "artifact.archived", "artifact.released"},
}


def supports_work_item_summary(version):
    """Whether the named Gateway release promises the complete item summary."""
    return isinstance(version, dict) and version.get("version") == "0.1.15"


def has_work_item_summary(row):
    """Recognize the complete 0.1.15 summary shape without inferring missing data."""
    if not isinstance(row, dict) or not WORK_ITEM_SUMMARY_FIELDS <= row.keys():
        return False
    holders = row["holders"]
    started = row["started"]
    attest_counts = row["attestCounts"]
    verdict_counts = row["verdictCounts"]
    refs = row["effectiveCommitRefs"]
    landings = row["landings"]
    if (not isinstance(holders, list) or any(not isinstance(holder, str) or not holder for holder in holders)
            or len(holders) != len(set(holders)) or not isinstance(started, bool)):
        return False

    def valid_counts(value):
        return (isinstance(value, dict) and all(
            isinstance(kind, str) and kind and isinstance(count, int)
            and not isinstance(count, bool) and count >= 0
            for kind, count in value.items()))

    if not valid_counts(attest_counts) or not valid_counts(verdict_counts):
        return False
    if (not isinstance(refs, list) or any(
            not isinstance(ref, dict) or not isinstance(ref.get("commit"), str)
            or not ref["commit"] or (ref.get("repo") is not None
                                      and not isinstance(ref.get("repo"), str))
            for ref in refs)):
        return False
    landing_fields = {"repository", "branch", "commit", "headCommit", "pullRequest", "observedAt"}
    for landing in landings if isinstance(landings, list) else ():
        if (not isinstance(landing, dict) or not landing_fields <= landing.keys()
                or any(not isinstance(landing.get(field), str) or not landing[field]
                       for field in ("repository", "branch", "commit", "headCommit"))
                or not isinstance(landing["pullRequest"], int)
                or isinstance(landing["pullRequest"], bool) or landing["pullRequest"] <= 0
                or not isinstance(landing["observedAt"], int)
                or isinstance(landing["observedAt"], bool) or landing["observedAt"] < 0):
            return False
    return isinstance(landings, list)


def required_fields(resource, payload, attest_fields, version=None):
    if resource == "attests":
        return frozenset(attest_fields)
    if resource == "work-items":
        summary_present = bool(WORK_ITEM_SUMMARY_FIELDS & payload.keys())
        if supports_work_item_summary(version) or summary_present:
            return WORK_ITEM_FIELDS | WORK_ITEM_SUMMARY_FIELDS
        return WORK_ITEM_FIELDS
    if resource == "decision requests":
        # Operator rows use a different, open public projection. The closed R7
        # shape and trigger cover these three kinds; other kinds use a read.
        if payload.get("kind") not in {"statute", "agent", "effort"}:
            return None
        return DECISION_FIELDS | ({"returnedBy", "returnReason", "returnedAt"}
                                  if payload["kind"] == "agent" else set())
    return {"work-items": WORK_ITEM_FIELDS, "sessions": SESSION_FIELDS,
            "artifacts": ARTIFACT_FIELDS}.get(resource)


def policies_for(_version):
    """Return payload policies without making Gateway identity a feature gate."""
    return PAYLOAD_POLICIES


def payload_policy_warning(version):
    """Warn when runtime payloads come from a build beyond the audited source."""
    if not isinstance(version, dict):
        return "Gateway payload compatibility is unverified; per-payload checks remain active."
    identity = (version.get("version"), version.get("build"), version.get("sha"))
    if identity != BUILD_ID:
        return "Gateway payload compatibility is unverified; per-payload checks remain active."
    return None

"""ClinicalTrials.gov API v2: fetching and parsing.

API docs: https://clinicaltrials.gov/data-api/api
Free, no key required. We page through results politely and retry on 429/5xx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .normalize import normalize_phases, parse_partial_date, status_group

log = logging.getLogger(__name__)

API_URL = "https://clinicaltrials.gov/api/v2/studies"
USER_AGENT = "NeuroScope/0.1 (research project; https://github.com/gosainansh8)"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,             # 1s, 2s, 4s, 8s, 16s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s


def iter_studies(condition: str | None = None, *, nct_ids: list[str] | None = None,
                 page_size: int = 200, max_studies: int | None = None,
                 pause_s: float = 0.3, session: requests.Session | None = None,
                 stats: dict | None = None) -> Iterator[dict]:
    """Yield raw study JSON objects, following pageToken.

    Query by condition, or fetch an exact set of studies with nct_ids.
    If `stats` is given it is filled with pages fetched and the API's total count.
    """
    if not condition and not nct_ids:
        raise ValueError("give a condition or nct_ids")
    session = session or _session()
    stats = stats if stats is not None else {}
    stats.setdefault("pages", 0)
    params: dict[str, Any] = {"pageSize": page_size, "format": "json", "countTotal": "true"}
    if condition:
        params["query.cond"] = condition
    if nct_ids:
        params["filter.ids"] = ",".join(nct_ids)
    yielded = 0
    while True:
        resp = session.get(API_URL, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        stats["pages"] += 1
        if "totalCount" in payload:
            stats["total_count"] = payload["totalCount"]
            log.info("CT.gov reports %s studies for %r", payload["totalCount"], condition)
        for study in payload.get("studies", []):
            yield study
            yielded += 1
            if max_studies and yielded >= max_studies:
                return
        token = payload.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token
        params.pop("countTotal", None)
        time.sleep(pause_s)


def iter_studies_from_jsonl(path: str) -> Iterator[dict]:
    """Replay studies saved earlier with --save-raw. Lets you re-run offline."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def content_hash(raw: dict) -> str:
    """Hash of the parts of a record that represent real content.

    derivedSection is excluded on purpose: CT.gov rewrites fields there
    (for example the data version date) on every release, which would make
    every record look changed on every run.
    """
    material = {k: v for k, v in raw.items() if k != "derivedSection"}
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class Intervention:
    name: str
    intervention_type: str | None
    description: str | None
    other_names: list[str]
    arm_group_labels: list[str]


@dataclass
class Outcome:
    kind: str
    measure: str
    description: str | None
    time_frame: str | None


@dataclass
class TrialRecord:
    nct_id: str
    brief_title: str
    official_title: str | None
    study_type: str | None
    overall_status: str
    status_group: str
    why_stopped: str | None
    phases: list[str]
    phase_label: str
    highest_phase: float | None
    enrollment: int | None
    enrollment_type: str | None
    allocation: str | None
    masking: str | None
    start_date: date | None
    start_date_precision: str | None
    primary_completion_date: date | None
    primary_completion_precision: str | None
    completion_date: date | None
    completion_precision: str | None
    last_update_posted: date | None
    has_results: bool
    lead_sponsor: tuple[str, str | None] | None
    collaborators: list[tuple[str, str | None]]
    conditions: list[str]
    keywords: list[str]
    interventions: list[Intervention] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)


def _g(d: dict | None, *path, default=None):
    """Safe nested get: _g(raw, 'protocolSection', 'statusModule', 'overallStatus')."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def parse_study(raw: dict) -> TrialRecord:
    ps = raw.get("protocolSection") or {}
    ident = ps.get("identificationModule") or {}
    status = ps.get("statusModule") or {}
    design = ps.get("designModule") or {}
    sponsors = ps.get("sponsorCollaboratorsModule") or {}
    arms = ps.get("armsInterventionsModule") or {}
    outcomes_mod = ps.get("outcomesModule") or {}
    cond_mod = ps.get("conditionsModule") or {}

    nct_id = ident.get("nctId")
    if not nct_id:
        raise ValueError("study has no nctId")

    overall = status.get("overallStatus") or "UNKNOWN"
    phases = design.get("phases") or []
    phase_label, highest = normalize_phases(phases)

    start, start_p = parse_partial_date(_g(status, "startDateStruct", "date"))
    pcd, pcd_p = parse_partial_date(_g(status, "primaryCompletionDateStruct", "date"))
    cd, cd_p = parse_partial_date(_g(status, "completionDateStruct", "date"))
    lup, _ = parse_partial_date(_g(status, "lastUpdatePostDateStruct", "date"))

    lead = sponsors.get("leadSponsor")
    lead_t = (lead["name"].strip(), lead.get("class")) if lead and lead.get("name") else None
    collabs = [
        (c["name"].strip(), c.get("class"))
        for c in sponsors.get("collaborators") or []
        if c.get("name")
    ]

    interventions = [
        Intervention(
            name=(iv.get("name") or "").strip(),
            intervention_type=iv.get("type"),
            description=iv.get("description"),
            other_names=[n.strip() for n in iv.get("otherNames") or [] if n and n.strip()],
            arm_group_labels=iv.get("armGroupLabels") or [],
        )
        for iv in arms.get("interventions") or []
        if (iv.get("name") or "").strip()
    ]

    outcomes = []
    for kind, key in (("primary", "primaryOutcomes"), ("secondary", "secondaryOutcomes")):
        for o in outcomes_mod.get(key) or []:
            if o.get("measure"):
                outcomes.append(Outcome(kind, o["measure"], o.get("description"), o.get("timeFrame")))

    enrollment = _g(design, "enrollmentInfo", "count")
    return TrialRecord(
        nct_id=nct_id,
        brief_title=ident.get("briefTitle") or nct_id,
        official_title=ident.get("officialTitle"),
        study_type=design.get("studyType"),
        overall_status=overall,
        status_group=status_group(overall),
        why_stopped=status.get("whyStopped"),
        phases=phases,
        phase_label=phase_label,
        highest_phase=highest,
        enrollment=int(enrollment) if enrollment is not None else None,
        enrollment_type=_g(design, "enrollmentInfo", "type"),
        allocation=_g(design, "designInfo", "allocation"),
        masking=_g(design, "designInfo", "maskingInfo", "masking"),
        start_date=start,
        start_date_precision=start_p,
        primary_completion_date=pcd,
        primary_completion_precision=pcd_p,
        completion_date=cd,
        completion_precision=cd_p,
        last_update_posted=lup,
        has_results=bool(raw.get("hasResults")),
        lead_sponsor=lead_t,
        collaborators=collabs,
        conditions=cond_mod.get("conditions") or [],
        keywords=cond_mod.get("keywords") or [],
        interventions=interventions,
        outcomes=outcomes,
    )

"""EMA (EU) authorisation loading, matching, and the agent tool. Real Postgres, synthetic records."""

import json

from neuroscope import tools
from neuroscope.agent import check_attribution, check_citations, ToolCall
from neuroscope.ema import load
from neuroscope.ingest import run_ingestion
from tests.fixtures import iv, study


def med(number, inn, *, name="Brandx", status="Authorised", authorised="15/04/2025", category="Human",
        indication="Treatment of something.", area="Some Disease", substance=None, **extra):
    return {"category": category, "name_of_medicine": name, "ema_product_number": number,
            "medicine_status": status, "international_non_proprietary_name_common_name": inn,
            "active_substance": substance if substance is not None else inn, "therapeutic_area_mesh": area,
            "therapeutic_indication": indication, "marketing_authorisation_date": authorised,
            "marketing_authorisation_developer_applicant_holder": "Test Pharma",
            "medicine_url": f"https://www.ema.europa.eu/en/medicines/human/EPAR/{name.lower()}", **extra}


def ema_file(tmp_path, records, stamp="2026-11-12T06:01:25Z"):
    path = tmp_path / "medicines.json"
    path.write_text(json.dumps({"meta": {"total_records": len(records), "timestamp": stamp}, "data": records}),
                    encoding="utf-8")
    return path


def stored(conn):
    return conn.execute(
        "SELECT e.ema_product_number, d.canonical_name, e.medicine_status, e.authorisation_date::text, "
        "e.match_method, e.indication_mentions_alzheimers FROM ema_medicines e JOIN drugs d ON d.id = e.drug_id "
        "ORDER BY 1").fetchall()


def test_load_matches_trusted_names_only_and_keeps_status(conn, tmp_path):
    # 'fakeumab' is an auto-created drug; 'ABC-100' is only its unreviewed alias.
    run_ingestion(conn, [study("NCT93000101", interventions=[iv("ABC-100", other_names=["fakeumab"])])],
                  "test", "synthetic")
    s = load(conn, ema_file(tmp_path, [
        med("EMEA/H/C/999001", "lecanemab", name="Leqembi", area="Cognitive Dysfunction;Alzheimer Disease",
            indication="Leqembi is indicated for the treatment of mild cognitive impairment&nbsp;due to "
                       "<p>Alzheimer&#39;s disease</p>."),
        med("EMEA/H/C/999002", "memantine", substance="memantine hydrochloride", name="Ebixa"),
        med("EMEA/H/C/999003", "aducanumab", name="Aduhelm", status="Application withdrawn", authorised="",
            withdrawal_of_application_date="22/04/2022"),
        med("EMEA/H/C/999004", "fakeumab"),                                  # own name: trusted
        med("EMEA/H/C/999005", "ABC-100"),                                   # unreviewed alias: no match
        med("EMEA/H/C/999006", "unknownol"),                                 # not one of our drugs
        med("EMEA/V/C/999007", "memantine", category="Veterinary"),           # animals: skipped
    ]))
    assert s == {"medicines_read": 6, "medicines_stored": 4, "authorised": 3, "drugs_matched": 4}
    assert stored(conn) == [
        ("EMEA/H/C/999001", "lecanemab", "Authorised", "2025-04-15", "exact", True),
        ("EMEA/H/C/999002", "memantine", "Authorised", "2025-04-15", "exact", False),
        ("EMEA/H/C/999003", "aducanumab", "Application withdrawn", None, "exact", False),
        ("EMEA/H/C/999004", "fakeumab", "Authorised", "2025-04-15", "exact", False),
    ]
    text = conn.execute("SELECT therapeutic_indication FROM ema_medicines WHERE name = 'Leqembi'").fetchone()[0]
    assert text == "Leqembi is indicated for the treatment of mild cognitive impairment due to Alzheimer's disease ."


def test_salt_radiolabel_and_combination_rules_match_fda(conn, tmp_path):
    run_ingestion(conn, [study("NCT93000102", interventions=[iv("Florbetapir F-18")])], "test", "synthetic")
    load(conn, ema_file(tmp_path, [
        med("EMEA/H/C/999010", "donepezil hydrochloride"),                   # salt word stripped
        med("EMEA/H/C/999011", "florbetapir (18F)"),                          # radiolabel notation
        med("EMEA/H/C/999012", "xanomeline tartrate;trospium chloride"),     # the combination drug
        med("EMEA/H/C/999013", "memantine hydrochloride;donepezil hydrochloride"),   # no such combination
    ]))
    assert [(r[0], r[1].lower(), r[4]) for r in stored(conn)] == [
        ("EMEA/H/C/999010", "donepezil", "normalized"),
        ("EMEA/H/C/999011", "[18f]florbetapir", "normalized"),
        ("EMEA/H/C/999012", "xanomeline + trospium", "normalized"),
    ]


def test_reload_replaces_rows(conn, tmp_path):
    load(conn, ema_file(tmp_path, [med("EMEA/H/C/999020", "lecanemab")]))
    load(conn, ema_file(tmp_path, [med("EMEA/H/C/999021", "donanemab")]))
    assert [r[0] for r in stored(conn)] == ["EMEA/H/C/999021"]


def test_tool_reports_eu_status_and_absence_honestly(conn, tmp_path):
    load(conn, ema_file(tmp_path, [
        med("EMEA/H/C/999030", "lecanemab", name="Leqembi", indication="For Alzheimer's disease."),
        med("EMEA/H/C/999031", "aducanumab", name="Aduhelm", status="Refused", authorised="",
            refusal_of_marketing_authorisation_date="17/12/2021"),
    ]))
    leq = tools.get_ema_status(conn, "lecanemab")
    assert leq["currently_authorised_in_eu"] is True
    assert leq["medicines"][0]["ema_product_number"] == "EMEA/H/C/999030"
    assert leq["medicines"][0]["indication_mentions_alzheimers"] is True
    adu = tools.get_ema_status(conn, "aducanumab")
    assert adu["currently_authorised_in_eu"] is False
    assert adu["medicines"][0]["refusal_date"] == "2021-12-17"
    none = tools.get_ema_status(conn, "donepezil")
    assert none["ema_record"] is False and "country by country" in none["caveat"]
    assert tools.get_ema_status(conn, "notadrugatall")["found"] is False
    pipeline = tools.list_pipeline(conn, min_phase=0, active_only=False)
    assert "ema_authorised_medicine" in pipeline["caveat"]


def test_ema_numbers_are_checked_and_attributed(conn, tmp_path):
    load(conn, ema_file(tmp_path, [med("EMEA/H/C/999040", "lecanemab", name="Leqembi"),
                                   med("EMEA/H/C/999041", "donanemab", name="Kisunla")]))
    steps = [(ToolCall("get_ema_status", {"drug": d}), tools.get_ema_status(conn, d))
             for d in ("lecanemab", "donanemab")]
    cited, unsupported = check_citations(
        "Leqembi is authorised in the EU (EMEA/H/C/999040). Also EMEA/H/C/123456.", steps)
    assert cited["ema_product"] == ["EMEA/H/C/999040"]
    assert unsupported["ema_product"] == ["EMEA/H/C/123456"]
    warnings = check_attribution("Lecanemab is authorised in the EU (EMEA/H/C/999041).", steps)
    assert [w["identifier"] for w in warnings] == ["EMEA/H/C/999041"]

"""FDA approval loading and matching. Real Postgres, synthetic applications (fake 999xxx numbers)."""

import json

import pytest

from neuroscope.fda import ingredient_forms, load, parse_application
from neuroscope.ingest import run_ingestion
from tests.fixtures import iv, study


def app(number, ingredients, *, brand="BRANDX", orig=("AP", "20230106"), marketing="Prescription",
        sponsor="TEST PHARMA", extra_submissions=()):
    subs = [{"submission_type": "SUPPL", "submission_number": "2", "submission_status": "AP",
             "submission_status_date": "20240501"}]
    if orig:
        subs.append({"submission_type": "ORIG", "submission_number": "1", "submission_status": orig[0],
                     "submission_status_date": orig[1]})
    subs += list(extra_submissions)
    return {"application_number": number, "sponsor_name": sponsor, "submissions": subs,
            "products": [{"product_number": "001", "brand_name": brand, "marketing_status": marketing,
                          "dosage_form": "TABLET", "route": "ORAL",
                          "active_ingredients": [{"name": n, "strength": "10MG"} for n in ingredients]}]}


def export(tmp_path, apps, updated="2026-11-06"):
    path = tmp_path / "drugsfda.json"
    path.write_text(json.dumps({"meta": {"last_updated": updated}, "results": apps}), encoding="utf-8")
    return path


def approval(conn, drug):
    return conn.execute(
        "SELECT first_approval_date::text, n_new_drug_applications, n_generic_applications, "
        "currently_marketed, brand_names FROM v_drug_fda_approval WHERE canonical_name = %s", (drug,)).fetchone()


def matched(conn):
    return conn.execute(
        "SELECT p.application_number, d.canonical_name, p.matched_key, p.match_method FROM fda_products p "
        "JOIN drugs d ON d.id = p.drug_id ORDER BY 1").fetchall()


@pytest.mark.parametrize("name,forms", [
    ("LECANEMAB-IRMB", ["LECANEMAB-IRMB", "LECANEMAB"]),
    ("DONEPEZIL HYDROCHLORIDE", ["DONEPEZIL HYDROCHLORIDE", "DONEPEZIL"]),
    ("FLORBETAPIR F-18", ["FLORBETAPIR F-18", "[18F]FLORBETAPIR"]),
    ("TROSPIUM CHLORIDE", ["TROSPIUM CHLORIDE", "TROSPIUM"]),
    ("MEDROXYPROGESTERONE ACETATE", ["MEDROXYPROGESTERONE ACETATE"]),   # an ester is kept
    ("SODIUM CHLORIDE", ["SODIUM CHLORIDE"]),                           # never reduced to a salt word
])
def test_ingredient_forms(name, forms):
    assert ingredient_forms(name) == forms


def test_parse_application_statuses():
    assert parse_application(app("BLA999001", ["X"])).approval_status == "approved"
    assert parse_application(app("BLA999001", ["X"])).original_approval_date.isoformat() == "2023-01-06"
    tentative = parse_application(app("ANDA999002", ["X"], orig=("TA", "20220101")))
    assert (tentative.approval_status, tentative.original_approval_date) == ("tentative", None)
    assert parse_application(app("NDA999003", ["X"], orig=None)).approval_status == "no_submission_record"
    with pytest.raises(ValueError):
        parse_application({"application_number": "XYZ1"})


def test_load_matches_by_trusted_names_only(conn, tmp_path):
    # 'fakeumab' is an auto-created drug; 'ABC-100' is only its unreviewed alias.
    run_ingestion(conn, [study("NCT93000001", interventions=[iv("ABC-100", other_names=["fakeumab"])])],
                  "test", "synthetic")
    s = load(conn, export(tmp_path, [
        app("BLA999001", ["LECANEMAB-IRMB"], brand="LEQEMBI"),                # biologic suffix
        app("NDA999002", ["DONEPEZIL HYDROCHLORIDE"], brand="ARICEPT", orig=("AP", "19961125")),
        app("ANDA999003", ["DONEPEZIL HYDROCHLORIDE"], brand="DONEPEZIL", orig=("AP", "20101130")),
        app("NDA999004", ["FAKEUMAB"]),                                       # drug's own name: trusted
        app("NDA999005", ["ABC-100"]),                                        # unreviewed alias: no match
        app("NDA999006", ["UNKNOWNOL"]),                                      # not one of our drugs
        {"application_number": "BAD"},                                        # unparseable, skipped alone
    ]))
    assert s == {"applications_read": 7, "applications_stored": 4, "products_stored": 4,
                 "drugs_matched": 3, "unparseable": 1}
    assert matched(conn) == [
        ("ANDA999003", "donepezil", "donepezil", "normalized"),
        ("BLA999001", "lecanemab", "lecanemab", "normalized"),
        ("NDA999002", "donepezil", "donepezil", "normalized"),
        ("NDA999004", "fakeumab", "fakeumab", "exact"),
    ]
    assert approval(conn, "lecanemab") == ("2023-01-06", 1, 0, True, ["LEQEMBI"])
    # First approval comes from the NDA, not the later generic.
    assert approval(conn, "donepezil") == ("1996-11-25", 1, 1, True, ["ARICEPT"])
    # Published ingredient text and the raw record are kept.
    assert conn.execute("SELECT active_ingredients, raw->>'sponsor_name' FROM fda_products p JOIN "
                        "fda_applications a USING (application_number) WHERE application_number = 'BLA999001'"
                        ).fetchone() == (["LECANEMAB-IRMB"], "TEST PHARMA")


def test_combination_product_matches_only_the_combination(conn, tmp_path):
    load(conn, export(tmp_path, [
        app("NDA999010", ["TROSPIUM CHLORIDE", "XANOMELINE TARTRATE"], brand="COBENFY"),
        app("NDA999011", ["DONEPEZIL HYDROCHLORIDE", "MEMANTINE HYDROCHLORIDE"], brand="NAMZARIC"),
    ]))
    # Cobenfy matches the seeded combination. Namzaric matches nothing: there is no
    # donepezil + memantine drug yet, and it must never fall back to donepezil or memantine.
    assert matched(conn) == [("NDA999010", "xanomeline + trospium", "combo:trospium+xanomeline", "normalized")]
    assert approval(conn, "donepezil") is None and approval(conn, "memantine") is None


def test_ingredient_that_is_only_a_salt_word_does_not_break_the_load(conn, tmp_path):
    # Real exports contain multi-ingredient products such as CALCIUM; MAGNESIUM; POTASSIUM.
    assert ingredient_forms("CALCIUM") == []
    s = load(conn, export(tmp_path, [app("NDA999012", ["CALCIUM", "MAGNESIUM", "POTASSIUM CHLORIDE"]),
                                     app("NDA999013", ["CALCIUM"])]))
    assert (s["applications_read"], s["applications_stored"]) == (2, 0)


def test_tentative_or_discontinued_is_reported_honestly(conn, tmp_path):
    load(conn, export(tmp_path, [
        app("ANDA999020", ["MEMANTINE HYDROCHLORIDE"], orig=("TA", "20120101"), marketing="None (Tentative Approval)"),
        app("BLA999021", ["ADUCANUMAB-AVWA"], brand="ADUHELM", orig=("AP", "20210607"), marketing="Discontinued"),
    ]))
    # A tentative approval is stored but is not an approval.
    assert conn.execute("SELECT approval_status FROM fda_applications WHERE application_number = 'ANDA999020'"
                        ).fetchone()[0] == "tentative"
    assert approval(conn, "memantine") is None
    # Approved once, no longer marketed.
    assert approval(conn, "aducanumab") == ("2021-06-07", 1, 0, False, ["ADUHELM"])


def test_approval_is_never_inferred_from_trials(conn, tmp_path):
    # A completed Phase 4 trial with results, and no FDA record.
    run_ingestion(conn, [study("NCT93000002", phases=["PHASE4"], status="COMPLETED", has_results=True,
                               interventions=[iv("Masitinib")])], "test", "synthetic")
    load(conn, export(tmp_path, [app("NDA999030", ["UNKNOWNOL"])]))
    assert conn.execute("SELECT highest_phase_any FROM v_drug_pipeline WHERE canonical_name = 'masitinib'"
                        ).fetchone()[0] == 4
    assert approval(conn, "masitinib") is None


def test_reload_replaces_rows(conn, tmp_path):
    load(conn, export(tmp_path, [app("BLA999001", ["LECANEMAB-IRMB"]), app("NDA999002", ["DONEPEZIL HYDROCHLORIDE"])]))
    load(conn, export(tmp_path, [app("BLA999001", ["LECANEMAB-IRMB"])], updated="2026-11-13"))
    assert conn.execute("SELECT application_number, source_export_date::text FROM fda_applications"
                        ).fetchall() == [("BLA999001", "2026-11-13")]
    assert conn.execute("SELECT COUNT(*) FROM fda_products").fetchone()[0] == 1


# --- Labels and indications ----------------------------------------------------

from neuroscope.fda import load_labels, parse_label  # noqa: E402


def label(set_id, numbers, text, *, brand="BRANDX", effective="20260101"):
    raw = {"set_id": set_id, "effective_time": effective,
           "openfda": {"application_number": numbers, "brand_name": [brand]}}
    if text is not None:
        raw["indications_and_usage"] = [text]
    return raw


def test_parse_label_keeps_text_and_flags_mentions():
    l = parse_label(label("s1", ["BLA999001"], "1 INDICATIONS AND USAGE X is indicated for the treatment of Alzheimer’s disease."))
    assert l.indications_mention_alzheimers is True and l.effective_date.isoformat() == "2026-01-01"
    assert l.indications_and_usage.endswith("Alzheimer’s disease.")
    assert parse_label(label("s2", ["NDA1"], "Y is indicated for type 2 diabetes mellitus.")).indications_mention_alzheimers is False
    missing = parse_label(label("s3", ["NDA1"], None))
    assert (missing.indications_and_usage, missing.indications_mention_alzheimers) == (None, False)
    with pytest.raises(ValueError):
        parse_label({"openfda": {}})


def test_labels_tell_approved_for_alzheimers_from_approved_for_something_else(conn, tmp_path):
    load(conn, export(tmp_path, [
        app("BLA999001", ["LECANEMAB-IRMB"], brand="LEQEMBI"),
        app("NDA999040", ["SEMAGLUTIDE"], brand="OZEMPIC", orig=("AP", "20171205")),
        app("NDA999041", ["SUVOREXANT"], brand="BELSOMRA", orig=("AP", "20140813")),
        # Donepezil: marketed brand application, so generic labels are not used.
        app("NDA999043", ["DONEPEZIL HYDROCHLORIDE"], brand="ARICEPT", orig=("AP", "19961125")),
        app("ANDA999042", ["DONEPEZIL HYDROCHLORIDE"], orig=("AP", "20101130")),
        # Galantamine: brand discontinued (the real Razadyne case), so its generic label is used.
        app("NDA999044", ["GALANTAMINE HYDROBROMIDE"], brand="RAZADYNE", orig=("AP", "20010228"), marketing="Discontinued"),
        app("ANDA999045", ["GALANTAMINE HYDROBROMIDE"], orig=("AP", "20080828")),
    ]))
    s = load_labels(conn, iter([
        label("set-leq", ["BLA999001"], "LEQEMBI is indicated for the treatment of Alzheimer's disease.", brand="LEQEMBI"),
        label("set-oze", ["NDA999040"], "OZEMPIC is indicated as an adjunct to diet and exercise in type 2 diabetes mellitus."),
        label("set-generic", ["ANDA999042"], "Donepezil is indicated for dementia of the Alzheimer's type."),  # brand is marketed
        label("set-gal", ["ANDA999045"], "Galantamine is indicated for dementia of the Alzheimer's type."),
        label("set-other", ["NDA000000"], "Unrelated product."),                                              # not stored
        {"effective_time": "20260101"},                                                                       # unparseable
    ]))
    assert s == {"labels_read": 6, "labels_stored": 3, "mention_alzheimers": 2, "unparseable": 1}

    def status(drug):
        return conn.execute("SELECT n_labels, indications_mention_alzheimers FROM v_drug_fda_approval "
                            "WHERE canonical_name = %s", (drug,)).fetchone()
    assert status("lecanemab") == (1, True)
    assert status("semaglutide") == (1, False)          # approved, but not for Alzheimer's
    assert status("suvorexant") == (0, None)            # approved; no label retrieved, so unknown
    assert status("donepezil") == (0, None)             # generic label ignored while the brand is marketed
    assert status("galantamine") == (1, True)           # generic label used because the brand is discontinued
    assert conn.execute("SELECT indications_and_usage FROM fda_labels WHERE set_id = 'set-leq'"
                        ).fetchone()[0] == "LEQEMBI is indicated for the treatment of Alzheimer's disease."
    # Reloading replaces the labels.
    load_labels(conn, iter([]))
    assert status("lecanemab") == (0, None)

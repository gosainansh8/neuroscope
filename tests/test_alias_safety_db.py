"""Alias safety, combinations, corrections, and updates. Real Postgres, synthetic data."""

import pytest

from neuroscope.curate import apply_file, map_alias, reject_alias, reresolve, verify_alias
from neuroscope.ingest import run_ingestion
from neuroscope.review import build_review
from tests.fixtures import iv, study


def ingest(conn, studies):
    return run_ingestion(conn, studies, "test", "synthetic")


def links(conn, nct_id):
    """[(original name, resolved drug or None, method)] for one trial."""
    return conn.execute(
        "SELECT ti.name, d.canonical_name, ti.resolution_method FROM trial_interventions ti "
        "LEFT JOIN drugs d ON d.id = ti.drug_id WHERE ti.nct_id = %s ORDER BY ti.id",
        (nct_id,)).fetchall()


def alias_owner(conn, key):
    row = conn.execute(
        "SELECT d.canonical_name, a.review_status FROM drug_aliases a "
        "LEFT JOIN drugs d ON d.id = a.drug_id WHERE a.alias_key = %s", (key,)).fetchone()
    return row


# --- Ambiguity -------------------------------------------------------------

def test_ambiguous_names_without_registered_match_stay_unlinked(conn):
    # Registered name is unknown; its otherNames point at two different known drugs.
    ingest(conn, [study("NCT91000001", interventions=[iv("XYZ-999", other_names=["Lecanemab", "Aducanumab"])])])
    assert links(conn, "NCT91000001") == [("XYZ-999", None, "conflict")]
    n = conn.execute("SELECT COUNT(*) FROM drug_alias_conflicts WHERE nct_id = 'NCT91000001'").fetchone()[0]
    assert n == 2
    # Nothing is learned from an ambiguous record.
    assert alias_owner(conn, "xyz999") is None


def test_two_interventions_in_one_trial_never_merge(conn):
    ingest(conn, [study("NCT91000002", interventions=[iv("AAA-111"), iv("BBB-222")])])
    drugs = [d for _, d, _ in links(conn, "NCT91000002")]
    assert drugs == ["AAA-111", "BBB-222"]


def test_shared_words_do_not_link(conn):
    # 'Lecanemab subcutaneous' is cleaned to 'lecanemab', but 'Lecanemab biosimilar'
    # is a different string and must not match by shared word.
    ingest(conn, [study("NCT91000003", interventions=[iv("Lecanemab biosimilar", "BIOLOGICAL")])])
    assert links(conn, "NCT91000003") == [("Lecanemab biosimilar", "lecanemab biosimilar", "created")]


# --- Combinations ----------------------------------------------------------

def test_combination_is_its_own_drug_with_component_links(conn):
    ingest(conn, [
        study("NCT91000010", interventions=[iv("Donepezil + Memantine")]),
        study("NCT91000011", interventions=[iv("Memantine and Donepezil 10 mg")]),
    ])
    assert links(conn, "NCT91000010") == [("Donepezil + Memantine", "donepezil + memantine", "created")]
    # Same order-independent key as the drug's own name: links without review.
    assert links(conn, "NCT91000011") == [("Memantine and Donepezil 10 mg", "donepezil + memantine", "matched")]
    comps = conn.execute(
        "SELECT c.canonical_name FROM drug_components dc JOIN drugs d ON d.id = dc.drug_id "
        "JOIN drugs c ON c.id = dc.component_drug_id WHERE d.canonical_name = 'donepezil + memantine' "
        "ORDER BY 1").fetchall()
    assert comps == [("donepezil",), ("memantine",)]
    # The ingredient aliases still belong to the single drugs.
    assert alias_owner(conn, "donepezil") == ("donepezil", "verified")


def test_combination_and_single_names_do_not_cross_link(conn):
    ingest(conn, [
        study("NCT91000012", interventions=[iv("Donepezil", other_names=["Donepezil + Memantine"])]),
        study("NCT91000013", interventions=[iv("Donepezil + Memantine", other_names=["Donepezil", "DM-77"])]),
    ])
    assert links(conn, "NCT91000012") == [("Donepezil", "donepezil", "matched")]
    assert links(conn, "NCT91000013")[0][1] == "donepezil + memantine"
    # 'DM-77' is a plain code on a combination record: it names the combination.
    assert alias_owner(conn, "dm77") == ("donepezil + memantine", "unreviewed")


def test_combination_ignores_single_drug_brand_in_other_names(conn):
    # 'Aricept' is donepezil's brand. On a combination record it must not
    # pull the combination onto donepezil, and must stay owned by donepezil.
    ingest(conn, [study("NCT91000016", interventions=[iv("Donepezil + Memantine", other_names=["Aricept"])])])
    assert links(conn, "NCT91000016") == [("Donepezil + Memantine", "donepezil + memantine", "created")]
    assert alias_owner(conn, "aricept") == ("donepezil", "verified")


def test_seeded_combination_matches_brand_and_ingredient_forms(conn):
    ingest(conn, [
        study("NCT91000014", interventions=[iv("KarXT")]),
        study("NCT91000015", interventions=[iv("Xanomeline and Trospium")]),
    ])
    assert links(conn, "NCT91000014")[0][1] == "xanomeline + trospium"
    assert links(conn, "NCT91000015")[0][1] == "xanomeline + trospium"


# --- Updates to existing trials ---------------------------------------------

def test_updated_trial_replaces_interventions_and_logs_changes(conn):
    ingest(conn, [study("NCT91000020", phases=["PHASE2"], interventions=[iv("BAN2401", "BIOLOGICAL"), iv("Placebo")])])
    s2 = ingest(conn, [study("NCT91000020", phases=["PHASE3"],
                             interventions=[iv("Lecanemab", "BIOLOGICAL"), iv("Donepezil")])])
    assert (s2.new, s2.changed) == (0, 1)
    assert links(conn, "NCT91000020") == [("Lecanemab", "lecanemab", "matched"),
                                          ("Donepezil", "donepezil", "matched")]
    changes = conn.execute("SELECT field, old_value, new_value FROM trial_changes "
                           "WHERE run_id = %s", (s2.run_id,)).fetchall()
    assert ("phase_label", "Phase 2", "Phase 3") in changes
    assert conn.execute("SELECT COUNT(*) FROM trial_snapshots WHERE nct_id = 'NCT91000020'").fetchone()[0] == 2


def test_missing_optional_fields_load(conn):
    bare = {"protocolSection": {"identificationModule": {"nctId": "NCT91000030"},
                                "statusModule": {"overallStatus": "RECRUITING"}}}
    s = ingest(conn, [bare])
    assert (s.new, s.failed) == (1, 0)
    row = conn.execute("SELECT brief_title, phase_label, enrollment, start_date, lead_sponsor_id "
                       "FROM trials WHERE nct_id = 'NCT91000030'").fetchone()
    assert row == ("NCT91000030", "Not applicable", None, None, None)


# --- Provenance and corrections ---------------------------------------------

def test_wrong_merge_is_reversible(conn, monkeypatch):
    # Opt back into trusting unreviewed aliases to reproduce a wrong merge; the
    # default would have left NCT91000041 pending review (see test below).
    monkeypatch.setenv("NEUROSCOPE_TRUST_UNREVIEWED_ALIASES", "1")
    ingest(conn, [
        study("NCT91000040", interventions=[iv("ABC-100", other_names=["fakeumab"])]),
        # Sponsor listed the wrong code as an otherName: DEF-200 is a different drug.
        study("NCT91000041", interventions=[iv("DEF-200", other_names=["ABC-100"])]),
    ])
    # The bad otherName caused a wrong link, and DEF-200 was learned onto fakeumab.
    assert links(conn, "NCT91000041") == [("DEF-200", "fakeumab", "matched")]
    assert alias_owner(conn, "def200") == ("fakeumab", "unreviewed")
    assert conn.execute("SELECT first_seen_nct FROM drug_aliases WHERE alias_key = 'def200'"
                        ).fetchone()[0] == "NCT91000041"

    n_iv = conn.execute("SELECT COUNT(*) FROM trial_interventions").fetchone()[0]
    map_alias(conn, "DEF-200", "defumab", note="sponsor pipeline lists DEF-200 as defumab")
    result = reresolve(conn)

    assert links(conn, "NCT91000040") == [("ABC-100", "fakeumab", "matched")]
    assert links(conn, "NCT91000041") == [("DEF-200", "defumab", "conflict")]
    assert result["relinked"] == 1
    # Nothing lost: same intervention rows, original names and otherNames intact.
    assert conn.execute("SELECT COUNT(*) FROM trial_interventions").fetchone()[0] == n_iv
    assert conn.execute("SELECT other_names FROM trial_interventions WHERE nct_id = 'NCT91000041'"
                        ).fetchone()[0] == ["ABC-100"]
    actions = [r[0] for r in conn.execute("SELECT action FROM alias_corrections ORDER BY id")]
    assert actions == ["map", "reresolve"]


def test_rejected_alias_is_never_relearned(conn):
    ingest(conn, [study("NCT91000050", interventions=[iv("Lecanemab", "BIOLOGICAL", ["LCN-1"])])])
    assert alias_owner(conn, "lcn1") == ("lecanemab", "unreviewed")
    reject_alias(conn, "LCN-1")
    ingest(conn, [study("NCT91000051", interventions=[iv("LCN-1", "BIOLOGICAL")])])
    assert alias_owner(conn, "lcn1") == (None, "rejected")
    # A rejected name is not a drug name: the intervention stays unlinked and no drug is created.
    assert links(conn, "NCT91000051") == [("LCN-1", None, "no_usable_name")]


def test_seed_aliases_are_verified(conn):
    assert alias_owner(conn, "ban2401") == ("lecanemab", "verified")


def test_origin_is_stamped_and_reported(conn):
    ingest(conn, [study("NCT91000060", interventions=[iv("Lecanemab", "BIOLOGICAL")])])
    assert conn.execute("SELECT data_origin FROM trials WHERE nct_id = 'NCT91000060'").fetchone()[0] == "synthetic"
    text = build_review(conn, limit=5)
    assert "SYNTHETIC TEST FIXTURE" in text and "clinicaltrials.gov/study/NCT91000060" not in text


# --- Unreviewed aliases are review candidates, not matches (the default) ------

def test_unreviewed_alias_does_not_resolve_other_trials(conn):
    ingest(conn, [
        study("NCT91000070", interventions=[iv("ABC-100", other_names=["fakeumab"])]),
        # A sponsor's wrong otherName. Under the old default this linked DEF-200 to fakeumab.
        study("NCT91000071", interventions=[iv("DEF-200", other_names=["ABC-100"])]),
    ])
    # The teaching trial still links to the drug it created.
    assert links(conn, "NCT91000070") == [("ABC-100", "fakeumab", "created")]
    # The other trial is left unlinked, pointing at the alias that needs review.
    assert links(conn, "NCT91000071") == [("DEF-200", None, "pending_review")]
    assert conn.execute("SELECT matched_alias_key, other_names FROM trial_interventions "
                        "WHERE nct_id = 'NCT91000071'").fetchone() == ("abc100", ["ABC-100"])
    # Nothing was learned from the pending record.
    assert alias_owner(conn, "def200") is None
    # Provenance of the candidate is preserved.
    assert conn.execute("SELECT source, review_status, first_seen_nct FROM drug_aliases "
                        "WHERE alias_key = 'abc100'").fetchone() == ("ctgov_name", "unreviewed", "NCT91000070")

    # Re-resolving keeps the teaching trial linked (its own aliases are trusted for it).
    reresolve(conn)
    assert links(conn, "NCT91000070") == [("ABC-100", "fakeumab", "matched")]
    assert links(conn, "NCT91000071") == [("DEF-200", None, "pending_review")]

    # Review workflow: a human verifies the alias, then reresolve links the waiting trial.
    verify_alias(conn, "ABC-100", note="checked sponsor pipeline")
    reresolve(conn)
    assert links(conn, "NCT91000071") == [("DEF-200", "fakeumab", "matched")]
    assert alias_owner(conn, "def200") == ("fakeumab", "unreviewed")
    actions = [r[0] for r in conn.execute("SELECT action FROM alias_corrections ORDER BY id")]
    assert actions == ["reresolve", "verify", "reresolve"]


def test_pending_alias_that_disagrees_is_logged_as_conflict(conn):
    ingest(conn, [
        study("NCT91000072", interventions=[iv("GHI-300", other_names=["ghiumab"])]),
        # Registered name is a verified seed; the otherName is an unreviewed alias of another drug.
        study("NCT91000073", interventions=[iv("Lecanemab", "BIOLOGICAL", ["GHI-300"])]),
    ])
    assert links(conn, "NCT91000073") == [("Lecanemab", "lecanemab", "conflict")]
    assert conn.execute("SELECT COUNT(*) FROM drug_alias_conflicts WHERE nct_id = 'NCT91000073'"
                        ).fetchone()[0] == 1


# --- Regressions from the live review (exact registered strings) --------------
# Each string below appeared in a live ClinicalTrials.gov record. NCT IDs here are fake.

def test_live_comma_name_code_pair_links_seeded_drug(conn):
    # Before: created a new drug named "Brexpiprazole, OPC-34712" although both names are seeded.
    ingest(conn, [study("NCT91000080", interventions=[iv("Brexpiprazole, OPC-34712")])])
    assert links(conn, "NCT91000080") == [("Brexpiprazole, OPC-34712", "brexpiprazole", "matched")]


def test_live_comma_combination_never_links_single_drug(conn):
    # Before: linked to rivastigmine alone via the 'Exelon' brand in parentheses.
    name = "Rivastigmine transdermal patch (Exelon patch), memantine"
    ingest(conn, [study("NCT91000081", interventions=[iv(name)])])
    assert links(conn, "NCT91000081") == [(name, "memantine + rivastigmine", "created")]
    assert alias_owner(conn, "exelon") == ("rivastigmine", "verified")


def test_live_drug_plus_bare_placebo_is_active(conn):
    # Before: all three were classified as placebo and left unlinked.
    ingest(conn, [study("NCT91000082", interventions=[
        iv("DAOIB plus placebo"), iv("Placebo and S-equol"), iv("S-equol and Placebo"),
        iv("Placebo to Match Emtricitabine 200mg/Tenofovir Alafenamide 25 mg."),
        iv("Huperzine A controlled-release tablets placebo; Donepezil hydrochloride tablets placebo"),
    ])])
    rows = conn.execute(
        "SELECT ti.name, d.canonical_name, ti.resolution_method, ti.is_placebo FROM trial_interventions ti "
        "LEFT JOIN drugs d ON d.id = ti.drug_id WHERE ti.nct_id = 'NCT91000082' ORDER BY ti.id").fetchall()
    assert rows == [
        ("DAOIB plus placebo", "daoib", "created", False),
        ("Placebo and S-equol", "s-equol", "created", False),
        ("S-equol and Placebo", "s-equol", "matched", False),
        # Placebos that describe what they match stay placebo, unlinked.
        ("Placebo to Match Emtricitabine 200mg/Tenofovir Alafenamide 25 mg.", None, "placebo", True),
        ("Huperzine A controlled-release tablets placebo; Donepezil hydrochloride tablets placebo",
         None, "placebo", True),
    ]


def test_live_isotope_label_is_never_an_alias(conn):
    # Before: '(18F)' was learned as alias '18f', which then linked
    # flutemetamol's code AH110690 to an unrelated tracer.
    ingest(conn, [
        study("NCT91000083", interventions=[iv("Florbetapir (18F)", other_names=["Florbetapir F 18", "Amyvid"])]),
        study("NCT91000084", interventions=[iv("AH110690 (18F) Injection")]),
    ])
    assert alias_owner(conn, "18f") is None
    assert links(conn, "NCT91000083") == [("Florbetapir (18F)", "[18F]Florbetapir", "created")]
    assert links(conn, "NCT91000084") == [("AH110690 (18F) Injection", "[18F]AH110690", "created")]


def test_live_isotope_notations_share_a_key_but_not_with_unlabeled(conn):
    ingest(conn, [
        study("NCT91000085", interventions=[iv("18F-MK-6240")]),
        study("NCT91000086", interventions=[iv("MK-6240")]),
    ])
    # Unlabeled MK-6240 is not merged with the labeled tracer; a human decides that.
    assert links(conn, "NCT91000086") == [("MK-6240", "MK-6240", "created")]
    ingest(conn, [study("NCT91000087", interventions=[iv("[18F]MK6240")])])
    assert conn.execute("SELECT matched_alias_key FROM trial_interventions WHERE nct_id = 'NCT91000087'"
                        ).fetchone()[0] == "18fmk6240"


def test_live_dose_percent_and_generic_parenthetical(conn):
    # Before: drug "Immune Globulin , %" with learned aliases "Human" and "IGIV, %".
    name = "Immune Globulin Intravenous (Human), 10% (IGIV, 10%) 200 mg/kg"
    ingest(conn, [study("NCT91000088", interventions=[iv(name, "BIOLOGICAL", ["Gammagard Liquid"])])])
    assert links(conn, "NCT91000088") == [(name, "immune globulin", "created")]
    assert alias_owner(conn, "human") is None
    assert alias_owner(conn, "igiv") == ("immune globulin", "unreviewed")


def test_live_arm_labels_and_procedures_are_not_aliases(conn):
    # Before: 'Treatment A' became an atabecestat alias; 'EEG', 'fMRI' and 'FDG-PET'
    # became insulin aliases, and a later FDG-PET intervention linked to insulin.
    ingest(conn, [
        study("NCT91000089", interventions=[iv("JNJ-54861911, 10 mg (Treatment A)")]),
        study("NCT91000090", interventions=[iv("Insulin", other_names=["EEG", "fMRI", "FDG-PET"])]),
        study("NCT91000091", interventions=[iv("FDG-PET", other_names=["fluorodeoxyglucose"])]),
    ])
    assert links(conn, "NCT91000089") == [("JNJ-54861911, 10 mg (Treatment A)", "atabecestat", "matched")]
    assert alias_owner(conn, "treatmenta") is None
    assert alias_owner(conn, "eeg") is None and alias_owner(conn, "fmri") is None
    assert links(conn, "NCT91000091") == [("FDG-PET", None, "pending_review")]


# --- Alternatives and display names -------------------------------------------

def test_live_alternatives_stay_unlinked_and_reviewable(conn):
    # Before: became one drug named for the whole phrase (and, earlier, 'ct scan').
    name = "18F-Florbetapir or 18F-Flutemetamol"
    n_drugs = conn.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]
    ingest(conn, [
        study("NCT91000100", interventions=[iv(name, other_names=["Amyvid or Vizamyl"])]),
        # An "A or B" otherName on an ordinary record is not learned either.
        study("NCT91000101", interventions=[iv("JKL-400", other_names=["Donepezil or Rivastigmine"])]),
    ])
    assert links(conn, "NCT91000100") == [(name, None, "ambiguous_name")]
    assert conn.execute("SELECT other_names FROM trial_interventions WHERE nct_id = 'NCT91000100'"
                        ).fetchone()[0] == ["Amyvid or Vizamyl"]
    assert conn.execute("SELECT COUNT(*) FROM drugs").fetchone()[0] == n_drugs + 1   # only JKL-400
    for key in ("18fflorbetapiror18fflutemetamol", "amyvidorvizamyl", "donepezilorrivastigmine"):
        assert alias_owner(conn, key) is None
    # Reviewable: listed by `curate ambiguous`, and unchanged by reresolve.
    reresolve(conn)
    assert links(conn, "NCT91000100") == [(name, None, "ambiguous_name")]


def test_live_description_does_not_become_canonical_name(conn):
    # Before: drug named "gamma secretase inhibitor".
    ingest(conn, [study("NCT91000102", interventions=[iv("BMS-708163", other_names=["gamma secretase inhibitor"])])])
    assert links(conn, "NCT91000102") == [("BMS-708163", "BMS-708163", "created")]
    # Both names are kept as aliases with their source and the record that taught them.
    rows = conn.execute("SELECT alias_display, source, review_status, first_seen_nct FROM drug_aliases "
                        "WHERE alias_key IN ('bms708163', 'gammasecretaseinhibitor') ORDER BY 1").fetchall()
    assert rows == [("BMS-708163", "ctgov_name", "unreviewed", "NCT91000102"),
                    ("gamma secretase inhibitor", "ctgov_other_name", "unreviewed", "NCT91000102")]


# --- Same-name rule: a registered name equal to a drug's own name links -------

def test_same_registered_name_links_without_review(conn):
    # Live case: 34 trials registered florbetapir; only the first was linked.
    ingest(conn, [
        study("NCT91000110", interventions=[iv("Florbetapir F 18", other_names=["Amyvid"])]),
        study("NCT91000111", interventions=[iv("Florbetapir (18F)")]),            # same name, other notation
        study("NCT91000112", interventions=[iv("Amyvid")]),                       # learned otherName
        study("NCT91000113", interventions=[iv("ZZZ-9", other_names=["Florbetapir F 18"])]),
    ])
    assert links(conn, "NCT91000110") == [("Florbetapir F 18", "[18F]Florbetapir", "created")]
    assert links(conn, "NCT91000111") == [("Florbetapir (18F)", "[18F]Florbetapir", "matched")]
    # A name the first trial only listed as an otherName still waits for review.
    assert links(conn, "NCT91000112") == [("Amyvid", None, "pending_review")]
    # The drug's own name arriving as an otherName does not qualify either.
    assert links(conn, "NCT91000113") == [("ZZZ-9", None, "pending_review")]
    assert alias_owner(conn, "zzz9") is None


def test_same_name_rule_needs_the_drugs_own_name(conn):
    # Drug is named 'fakeumab' (from the otherName); 'ABC-100' is only its unreviewed alias.
    ingest(conn, [
        study("NCT91000114", interventions=[iv("ABC-100", other_names=["fakeumab"])]),
        study("NCT91000115", interventions=[iv("ABC-100")]),
        study("NCT91000116", interventions=[iv("Fakeumab 10 mg")]),
    ])
    assert links(conn, "NCT91000115") == [("ABC-100", None, "pending_review")]
    assert links(conn, "NCT91000116") == [("Fakeumab 10 mg", "fakeumab", "matched")]


def test_reresolve_keeps_placebo_flag_in_step(conn):
    ingest(conn, [study("NCT91000117", interventions=[iv("Placebos"), iv("DAOIB plus placebo")])])
    conn.execute("UPDATE trial_interventions SET is_placebo = NOT is_placebo WHERE nct_id = 'NCT91000117'")
    reresolve(conn)
    assert conn.execute("SELECT name, resolution_method, is_placebo FROM trial_interventions "
                        "WHERE nct_id = 'NCT91000117' ORDER BY id").fetchall() == [
        ("Placebos", "placebo", True), ("DAOIB plus placebo", "matched", False)]


# --- Reviewed correction files -------------------------------------------------

def _corrections_file(tmp_path, body):
    path = tmp_path / "corrections.csv"
    path.write_text("action,alias,drug,confidence,reason\n" + body, encoding="utf-8")
    return str(path)


def test_apply_file_dry_run_changes_nothing_then_apply_links(conn, tmp_path):
    ingest(conn, [
        study("NCT91000120", interventions=[iv("Bapineuzumab", "BIOLOGICAL", ["AAB-001"])]),
        study("NCT91000121", interventions=[iv("AAB-001", "BIOLOGICAL")]),
        study("NCT91000122", interventions=[iv("Caffeine", other_names=["Midazolam"])]),
    ])
    assert links(conn, "NCT91000121") == [("AAB-001", None, "pending_review")]
    path = _corrections_file(tmp_path,
                             "map,AAB-001,bapineuzumab,high,sponsor code\n"
                             "map,Midazolam,midazolam,high,separate drug in a cocktail study\n")

    dry = apply_file(conn, path, dry_run=True)
    assert dry["applied"] is False and dry["after"].get("pending_review", 0) == 0
    assert links(conn, "NCT91000121") == [("AAB-001", None, "pending_review")]      # rolled back
    assert alias_owner(conn, "midazolam") == ("caffeine", "unreviewed")
    assert conn.execute("SELECT COUNT(*) FROM alias_corrections").fetchone()[0] == 0

    apply_file(conn, path)
    assert links(conn, "NCT91000121") == [("AAB-001", "bapineuzumab", "matched")]
    assert alias_owner(conn, "aab001") == ("bapineuzumab", "verified")
    assert alias_owner(conn, "midazolam") == ("midazolam", "verified")
    logged = conn.execute("SELECT action, alias_key, note FROM alias_corrections ORDER BY id").fetchall()
    assert logged[:2] == [("map", "aab001", "sponsor code"),
                          ("map", "midazolam", "separate drug in a cocktail study")]
    assert logged[2][0] == "reresolve"


def test_apply_file_is_all_or_nothing(conn, tmp_path):
    ingest(conn, [study("NCT91000123", interventions=[iv("Bapineuzumab", "BIOLOGICAL", ["AAB-001"])])])
    path = _corrections_file(tmp_path, "map,AAB-001,bapineuzumab,high,ok\nmerge,Foo,bar,high,bad action\n")
    with pytest.raises(ValueError, match="line 3"):
        apply_file(conn, path)
    assert alias_owner(conn, "aab001") == ("bapineuzumab", "unreviewed")


def test_apply_file_resolve_marks_conflicts_reviewed_and_survives_reresolve(conn, tmp_path):
    # A donepezil arm whose sponsor listed another drug's code as an otherName.
    ingest(conn, [
        study("NCT91000124", interventions=[iv("SB-742457")]),
        study("NCT91000125", interventions=[iv("Donepezil", other_names=["SB-742457"])]),
    ])
    assert links(conn, "NCT91000125") == [("Donepezil", "donepezil", "conflict")]
    path = _corrections_file(tmp_path, "resolve,NCT91000125,,high,donepezil arm of an SB-742457 study\n")
    r = apply_file(conn, path)
    assert (r["conflicts_resolved"], r["open_conflicts"]) == (1, 0)
    # The link is unchanged, and a later reresolve does not reopen the reviewed conflict.
    reresolve(conn)
    assert links(conn, "NCT91000125") == [("Donepezil", "donepezil", "conflict")]
    assert conn.execute("SELECT resolved FROM drug_alias_conflicts WHERE nct_id = 'NCT91000125'"
                        ).fetchall() == [(True,)]
    assert conn.execute("SELECT note FROM alias_corrections WHERE action = 'resolve_conflict'"
                        ).fetchone()[0].endswith("NCT91000125: donepezil arm of an SB-742457 study")


def test_rejected_name_never_names_or_creates_a_drug(conn):
    # Live case: NPT088 listed the class "anti-Abeta" as an otherName. After that
    # phrase was rejected, NPT088 was still named after it.
    reject_alias(conn, "anti-Abeta")
    ingest(conn, [
        study("NCT91000130", interventions=[iv("NPT088", "BIOLOGICAL", ["fusion protein", "anti-Abeta"])]),
        study("NCT91000131", interventions=[iv("anti-Abeta", "BIOLOGICAL")]),
    ])
    assert links(conn, "NCT91000130") == [("NPT088", "NPT088", "created")]
    assert links(conn, "NCT91000131") == [("anti-Abeta", None, "no_usable_name")]
    assert conn.execute("SELECT COUNT(*) FROM drugs WHERE canonical_name ILIKE 'anti-abeta'").fetchone()[0] == 0
    assert alias_owner(conn, "antiabeta") == (None, "rejected")


# --- Round-5 cleanup behavior ------------------------------------------------------

def test_brand_maps_to_combination_but_combination_never_to_single(conn, tmp_path):
    # Live case: "Namzaric" (donepezil + memantine) had been learned as an alias of donepezil alone.
    ingest(conn, [study("NCT91000140", interventions=[iv("Namzaric")])])
    map_alias(conn, "Namzaric", "donepezil + memantine", note="Namzaric is donepezil plus memantine")
    reresolve(conn)
    assert links(conn, "NCT91000140") == [("Namzaric", "donepezil + memantine", "matched")]
    assert alias_owner(conn, "namzaric") == ("donepezil + memantine", "verified")
    with pytest.raises(ValueError, match="combination name can only map to a combination"):
        map_alias(conn, "Donepezil + Memantine", "donepezil")


def test_class_action_marks_a_drug_after_reresolve_and_excludes_it(conn, tmp_path):
    from neuroscope import tools
    ingest(conn, [
        study("NCT91000141", phases=["PHASE3"], status="RECRUITING", interventions=[iv("Cholinesterase inhibitors")]),
        study("NCT91000142", phases=["PHASE3"], status="RECRUITING", interventions=[iv("Lecanemab", "BIOLOGICAL")]),
    ])
    path = _corrections_file(tmp_path, "class,cholinesterase inhibitors,,high,drug class\n")
    r = apply_file(conn, path)
    assert r["classes_marked"] == 1
    assert conn.execute("SELECT is_class FROM drugs WHERE canonical_name = 'cholinesterase inhibitors'"
                        ).fetchone()[0] is True
    pipeline = [d["drug"] for d in tools.list_pipeline(conn, min_phase=3)["drugs"]]
    assert "lecanemab" in pipeline and "cholinesterase inhibitors" not in pipeline
    found = tools.find_drug(conn, "cholinesterase inhibitors")
    assert found["is_class"] is True and "drug class" in found["note"]
    assert conn.execute("SELECT action FROM alias_corrections WHERE action = 'mark_class'").fetchone() == ("mark_class",)
    with pytest.raises(ValueError, match="no drug named"):
        apply_file(conn, _corrections_file(tmp_path, "class,no such class,,high,x\n"))

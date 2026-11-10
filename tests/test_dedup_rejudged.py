import evidence_log

Entry = next(
    c for c in vars(evidence_log).values()
    if isinstance(c, type) and isinstance(getattr(c, "dedup_key", None), property)
)

# Sterling margin finding as first logged on 2026-10-29 (judged on an old period)
WRONG_PERIOD = dict(
    accession="0000874238-26-000125", axis="StatementBusinessSegmentsAxis",
    comparison="yoy_ratio_change", computed=-0.0023582354497209135,
    concept="OperatingIncomeLoss", detail="-0.2pp in 2025-09-30 against a threshold of +0.0pp",
    filing_date="", form="", implied_detail="", implied_required=None,
    logged_at="2026-10-29T18:00:01+00:00", measure_id="S2-einfra-operating-margin",
    member="EInfrastructureSolutionsSegmentMember", period_end="2026-09-30",
    period_start="2026-07-01", prior_value=0.2556040910464007, schema_version=1,
    source="xbrl", thesis_id="strl-einfra-margin-2026h2", threshold=0.0,
    ticker="STRL", value=0.226073680146215, verdict="fired",
)


def test_same_verdict_on_a_different_number_is_a_new_finding():
    corrected = dict(WRONG_PERIOD, computed=-0.0295, detail="-3.0pp in 2026-09-30",
                     logged_at="2026-11-10T06:51:00+00:00")
    assert Entry(**WRONG_PERIOD).dedup_key != Entry(**corrected).dedup_key


def test_rerun_with_identical_result_is_still_a_duplicate():
    rerun = dict(WRONG_PERIOD, logged_at="2026-10-29T23:00:00+00:00")
    assert Entry(**WRONG_PERIOD).dedup_key == Entry(**rerun).dedup_key

from qasawatch.domain import EnrichedListing, ReasonSource
from qasawatch.cli import _filters
from qasawatch.filters import FilterChain, NumericRangeFilter, PredicateFilter
from qasawatch.schemas import FilterSettings


async def test_filter_chain_collects_machine_and_human_reasons_in_rule_order():
    listing = EnrichedListing("qasa", "https://example.test/1", "1", {"rent": 12_000})
    chain = FilterChain(
        [
            NumericRangeFilter("rent", maximum=10_300, name="budget"),
            PredicateFilter(
                name="manual_review",
                predicate=lambda _: False,
                code="human.not_a_fit",
                message="Not a fit after review",
                source=ReasonSource.HUMAN,
            ),
        ]
    )

    decision = await chain.evaluate(listing)

    assert not decision.accepted
    assert [reason.code for reason in decision.reasons] == [
        "rent.above_maximum",
        "human.not_a_fit",
    ]
    assert decision.reasons[1].source is ReasonSource.HUMAN


async def test_empty_filter_chain_accepts():
    listing = EnrichedListing("qasa", "https://example.test/1", "1", {})
    assert (await FilterChain().evaluate(listing)).accepted


async def test_boolean_attribute_requirements_are_tri_state_and_auditable():
    chain = _filters(
        FilterSettings(
            attribute_requirements={"furnished": True, "shared": False}
        )
    )

    accepted = await chain.evaluate(
        EnrichedListing(
            "qasa", "https://example.test/1", "1", {"furnished": True, "shared": False}
        )
    )
    rejected = await chain.evaluate(
        EnrichedListing(
            "qasa", "https://example.test/2", "2", {"furnished": False}
        )
    )

    assert accepted.accepted
    assert [reason.code for reason in rejected.reasons] == [
        "attribute.furnished.required_true",
        "attribute.shared.required_false",
    ]
    assert "missing or does not match" in rejected.reasons[1].message


async def test_foreign_background_percentage_filter_uses_scb_output_field():
    chain = _filters(
        FilterSettings(
            minimum_foreign_background_percent=20,
            maximum_foreign_background_percent=50,
        )
    )

    accepted = await chain.evaluate(
        EnrichedListing(
            "qasa",
            "https://example.test/1",
            "1",
            {"demographics": {"foreign_background_percent": 46.6}},
        )
    )
    rejected_high = await chain.evaluate(
        EnrichedListing(
            "qasa",
            "https://example.test/2",
            "2",
            {"demographics": {"foreign_background_percent": 51}},
        )
    )
    rejected_missing = await chain.evaluate(
        EnrichedListing("qasa", "https://example.test/3", "3", {})
    )

    assert accepted.accepted
    assert not rejected_high.accepted
    assert not rejected_missing.accepted
    assert rejected_high.reasons[0].code == (
        "demographic.foreign_background_percent_outside_range"
    )

from qasawatch.domain import EnrichedListing, ReasonSource
from qasawatch.filters import FilterChain, NumericRangeFilter, PredicateFilter


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

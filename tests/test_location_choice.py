"""A location box tries the saved place, then the city alone, then the profile's later places."""

from amorehiremepls.apply.applications import location_choice_line
from amorehiremepls.apply.browser import Plan, Step, apply_location_fallbacks, location_queries, suggestion_rank


def test_location_queries_try_the_city_then_each_later_place():
    assert location_queries("Lakeside, Northland", ["Harbor", "Capital", " Harbor "]) == ["Lakeside, Northland", "Lakeside", "Harbor", "Capital"]
    assert location_queries("Lakeside", ["Lakeside", "Harbor"]) == ["Lakeside", "Harbor"]
    assert location_queries("", []) == []


def test_a_short_region_code_matches_the_saved_region_and_a_different_region_does_not():
    assert suggestion_rank("Lakeside, Northland", "Lakeside, NO, Country") == 1
    assert suggestion_rank("Lakeside, Northland", "Lakeside, Northland, Country") == 2
    assert suggestion_rank("Lakeside, Northland", "Lakeside, GA, Country") is None
    assert suggestion_rank("Lakeside", "Lakeside, GA, Country") == 2
    assert suggestion_rank("Harbor", "Lakeside, NO, Country") is None


def test_later_places_attach_only_to_the_location_box():
    plan = Plan(
        start_url="https://boards.example/jobs/1",
        steps=[
            Step("autocomplete", "#location", "Lakeside, Northland", label="Location"),
            Step("autocomplete", "#country", "Country", label="Country", optional=True),
        ],
        receipt_text="Application submitted",
    )
    apply_location_fallbacks(plan, ["Harbor", "Capital"])
    assert plan.steps[0].fallbacks == ["Harbor", "Capital"]
    assert plan.steps[0].value == "Lakeside, Northland"
    assert plan.steps[1].fallbacks == []


def test_a_later_place_is_reported_and_the_saved_place_is_not():
    assert location_choice_line("Lakeside, Northland", "Lakeside, Northland", "Lakeside, NO, Country") is None
    assert location_choice_line("Lakeside, Northland", "Harbor", "Harbor, NO, Country") == (
        "The location list did not offer the saved place, so the form used Harbor, NO, Country."
    )

"""A location box tries the saved place, then the city alone, then the profile's later places."""

from datetime import UTC, datetime

from amorehiremepls.apply.applications import location_choice_line
from amorehiremepls.apply.browser import Plan, Step, apply_location_fallbacks, location_attempt_note, location_failure_reason, location_queries, step_note, suggestion_rank
from amorehiremepls.web.chrome import form_in_progress, situation_line


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


def test_what_to_do_now_is_the_latest_saved_step_and_names_the_place_that_was_typed():
    reason = location_failure_reason(["Lakeside, Northland", "Lakeside", "Harbor"])
    assert reason == "The location box did not show a place for Lakeside, Northland, then Lakeside, then Harbor"
    assert step_note(Step("fill", label="Phone")) == "Filling Phone"
    assert "secret" not in step_note(Step("choose", value="secret", label="Gender", sensitive=True))
    assert location_attempt_note("Location", "Lakeside", missed=True) == "Location: no places appeared for Lakeside"
    when = datetime(2026, 11, 6, tzinfo=UTC)
    later = datetime(2026, 11, 6, 0, 1, tzinfo=UTC)
    newest = datetime(2026, 11, 6, 0, 2, tzinfo=UTC)
    app = {"status": "failed", "history": [{"at": when, "status": "failed", "reason": "older"}]}
    runs = [
        {
            "status": "completed",
            "events": [
                {"at": later, "message": "Location: no places appeared for Lakeside"},
                {"at": newest, "message": "completed"},
            ],
        }
    ]
    assert situation_line(app, runs) == "Location: no places appeared for Lakeside"


def test_the_form_animation_runs_only_while_a_task_is_working():
    app = {"status": "authorized"}
    assert form_in_progress(app, [{"status": "queued"}]) is False
    assert form_in_progress(app, [{"status": "claimed"}]) is True
    assert form_in_progress(app, [{"status": "running"}]) is True
    assert form_in_progress({"status": "failed"}, [{"status": "running"}]) is False


def test_a_later_place_is_reported_and_the_saved_place_is_not():
    assert location_choice_line("Lakeside, Northland", "Lakeside, Northland", "Lakeside, NO, Country") is None
    assert location_choice_line("Lakeside, Northland", "Harbor", "Harbor, NO, Country") == (
        "The location list did not offer the saved place, so the form used Harbor, NO, Country."
    )

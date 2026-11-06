"""A location box tries the saved place, then the city alone, then the profile's later places."""

from datetime import UTC, datetime

from amorehiremepls.apply.applications import Answer, keep_typed_answers, location_choice_line
from amorehiremepls.apply.browser import (
    BrowserAdapter,
    BrowserConfig,
    Plan,
    Step,
    apply_location_fallbacks,
    exact_city_row,
    location_attempt_note,
    location_failure_reason,
    location_queries,
    step_note,
    suggestion_rank,
)
from amorehiremepls.web.chrome import FILLING_LINE, application_timeline, form_in_progress, shown_failure_reason, shown_status, shown_status_reason, situation_line, what_to_do_line


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
    exact = suggestion_rank("Lakeside", "Lakeside, Harbor, Country")
    beach = suggestion_rank("Lakeside", "Lakeside Beach, Harbor, Country")
    valley = suggestion_rank("Lakeside", "Lakeside Valley, Harbor, Country")
    island = suggestion_rank("Lakeside", "Lakesides Of Westshore Island, Harbor, Country")
    assert exact_city_row("Lakeside", "Lakeside, Harbor, Country")
    assert not exact_city_row("Lakeside", "Lakeside Beach, Harbor, Country")
    assert exact > beach and exact > valley and exact > island
    harbor = suggestion_rank("Lakeside", "Lakeside, Harbor, Country", "Lakeside, Harbor")
    westshore = suggestion_rank("Lakeside", "Lakeside, Westshore, Country", "Lakeside, Harbor")
    assert harbor > westshore


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
    assert reason == "The location box typed Lakeside, Northland, then Lakeside, then Harbor. No list appeared."
    offered = location_failure_reason(["Lakeside"], ["West Harbor", "East Harbor"])
    assert "Lakeside" in offered and "A list appeared" in offered and "West Harbor" in offered and "East Harbor" in offered
    assert step_note(Step("fill", label="Phone")) == "Filling Phone"
    assert "secret" not in step_note(Step("choose", value="secret", label="Gender", sensitive=True))
    assert location_attempt_note("Location", "Lakeside", missed=True) == "Location: no list appeared for Lakeside"
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
    assert what_to_do_line(app, runs) == "Location was typed as Lakeside. No list of places appeared, so the form is waiting."
    waiting = {
        "status": "failed",
        "review_package": {"answers": [{"field_id": "location", "label": "Location", "value": "Lakeside"}]},
        "history": [],
    }
    typed = [{"status": "completed", "events": [{"at": newest, "message": location_failure_reason(["Lakeside"])}]}]
    assert what_to_do_line(waiting, typed) == "Location was typed as Lakeside. No list of places appeared, so the form is waiting."
    filling = [{"status": "running"}]
    assert what_to_do_line({"status": "executing", "history": []}, filling) == FILLING_LINE


def test_a_failed_replay_says_the_form_was_not_opened_and_keeps_the_saved_reason():
    saved = "The location box did not show a place for Lakeside, Northland, then Lakeside"
    early = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
    failed_at = datetime(2026, 11, 6, 0, 1, tzinfo=UTC)
    done = datetime(2026, 11, 6, 0, 2, tzinfo=UTC)
    replay_at = datetime(2026, 11, 6, 0, 3, tzinfo=UTC)
    app = {
        "status": "failed",
        "material_hash": "m-same",
        "reason": "recorded outcome (replay)",
        "history": [
            {"at": failed_at, "status": "failed", "reason": saved},
            {"at": replay_at, "status": "failed", "reason": "recorded outcome (replay)"},
        ],
    }
    runs = [{"status": "completed", "events": [{"at": early, "message": "Location: no places appeared for Lakeside"}, {"at": done, "message": "completed"}]}]
    action = {"outcome_reason": saved, "status": "failed", "material_hash": "m-same"}
    assert shown_failure_reason(app, action) == saved
    assert situation_line(app, runs, saved) == "The form was not opened. Showing the result already saved."
    assert "recorded outcome" not in situation_line(app, runs, saved)
    newer = datetime(2026, 11, 6, 0, 4, tzinfo=UTC)
    later = [{"status": "completed", "events": [{"at": newer, "message": "Location: no places appeared for Lakeside"}]}]
    assert situation_line(app, later, saved) == "Location: no places appeared for Lakeside"
    rows = application_timeline(app, runs, saved)
    assert all("recorded outcome" not in row["text"] for row in rows)
    assert any(row["text"] == f"Applying failed. {saved}" for row in rows)
    assert shown_status({**app, "status": "authorized"}, action, []) == "failed"
    assert "recorded outcome" not in shown_status_reason(app, action, "failed")
    assert shown_status({"status": "authorized", "material_hash": "m-new"}, action, []) == "authorized"
    assert shown_status({"status": "submitted", "material_hash": "m-same"}, action, []) == "submitted"
    filling = [{"status": "running"}]
    assert shown_status({"status": "executing", "material_hash": "m-same"}, {"status": "executing", "material_hash": "m-same"}, filling) == "executing"


def test_a_typed_answer_replaces_the_profile_default_for_every_field():
    previous = [
        {"field_id": "old-location", "label": "Location (City)", "value": "Harbor", "fact_ids": ["user:input"], "needs_input": False, "reason": "edited by you"},
        {"field_id": "phone", "label": "Phone", "value": None, "fact_ids": ["user:input"], "needs_input": True, "reason": "cleared by you"},
        {"field_id": "note", "label": "Note for the team", "value": "Ready to start", "fact_ids": ["user:input"], "needs_input": False, "reason": "supplied by you"},
    ]
    fresh = [
        Answer("location", "Location (City)", "Lakeside, Northland", ["fact_city", "fact_region"], False, ""),
        Answer("phone", "Phone", "555 000 0000", ["fact_phone"], False, ""),
        Answer("note", "Note for the team", None, [], True, "no confirmed fact maps to this question"),
        Answer("email", "Email", "ada@example.com", ["fact_email"], False, ""),
        Answer("resume", "Resume", "/tmp/resume.pdf", ["artifact"], False, ""),
    ]
    keep_typed_answers(previous, fresh)
    by_id = {a.field_id: a for a in fresh}
    assert by_id["location"].value == "Harbor" and by_id["location"].fact_ids == ["user:input"]
    assert by_id["phone"].value is None and by_id["phone"].needs_input
    assert by_id["note"].value == "Ready to start"
    assert by_id["email"].value == "ada@example.com" and by_id["email"].fact_ids == ["fact_email"]
    assert by_id["resume"].value == "/tmp/resume.pdf" and by_id["resume"].fact_ids == ["artifact"]


def test_the_form_animation_runs_only_while_a_task_is_working():
    app = {"status": "authorized"}
    assert form_in_progress(app, [{"status": "queued"}]) is False
    assert form_in_progress(app, [{"status": "claimed"}]) is True
    assert form_in_progress(app, [{"status": "running"}]) is True
    assert form_in_progress({"status": "failed"}, [{"status": "running"}]) is False


class _Empty:
    def count(self):
        return 0

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self

    def all_inner_texts(self):
        return []

    def nth(self, index):
        return self

    def click(self, timeout=None):
        raise AssertionError("clicked an empty list")


class _Row:
    def __init__(self, menu, index: int) -> None:
        self.menu = menu
        self.index = index

    def click(self, timeout=None):
        self.menu.clicked.append(self.menu.rows[self.index])


class _Menu:
    def __init__(self) -> None:
        self.rows: list[str] = []
        self.clicked: list[str] = []

    def count(self):
        return len(self.rows)

    @property
    def first(self):
        return self

    def nth(self, index):
        return _Row(self, index)

    def all_inner_texts(self):
        return list(self.rows)

    def locator(self, selector):
        return self


class _Field:
    def __init__(self, frame) -> None:
        self.frame = frame
        self.typed: list[str] = []
        self.current = ""

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def evaluate(self, script):
        return "combobox"

    def click(self, timeout=None):
        return None

    def press(self, key):
        return None

    def press_sequentially(self, text, delay=0, timeout=None):
        self.typed.append(text)
        self.current = text

    def locator(self, selector):
        return _Empty()

    def get_attribute(self, name):
        return None


class _Frame:
    def __init__(self, field: _Field) -> None:
        self.field = field
        self.menu = _Menu()
        self.show = True

    def locator(self, selector):
        if "option" in selector or "pac-item" in selector or "listbox" in selector:
            typed = self.field.current
            if not self.show:
                self.menu.rows = []
            elif typed == "Lakeside":
                self.menu.rows = [
                    "Lakeside Beach, Harbor, Country",
                    "Lakeside Valley, Harbor, Country",
                    "Lakesides Of Westshore Island, Harbor, Country",
                    "Lakeside, Westshore, Country",
                    "Lakeside, Harbor, Country",
                ]
            elif typed == "West Harbor":
                self.menu.rows = ["West Harbor, Harbor, Country"]
            else:
                self.menu.rows = []
            return self.menu
        return _Empty()


class _Page:
    def __init__(self, field: _Field, decoy: bool = False) -> None:
        self.field = field
        self.decoy = decoy
        self.menu = _Menu()

    def locator(self, selector):
        if "option" in selector or "pac-item" in selector or "listbox" in selector:
            if self.decoy and self.field.current == "Lakeside":
                self.menu.rows = ["Lakeside, Westshore, Country"]
            else:
                self.menu.rows = []
            return self.menu
        return self.field

    def get_by_label(self, *args, **kwargs):
        return self.field

    def wait_for_timeout(self, ms):
        return None


class _Scope:
    def __init__(self, field: _Field, decoy: bool = False) -> None:
        self.field = field
        self.frame_selector = None
        self.page = _Page(field, decoy)

    def locator(self, selector):
        return self.page.locator(selector)

    def get_by_label(self, *args, **kwargs):
        return self.field

    def body_text(self):
        return ""

    def current_url(self):
        return "https://boards.example/apply"


class _Hold:
    def __init__(self, answer: str, frame: _Frame) -> None:
        self.answer = answer
        self.frame = frame
        self.minutes = 5
        self.poll_seconds = 0.01
        self.beats: list[dict] = []
        self.finished = None

    def heartbeat(self, evidence=None):
        self.beats.append(dict(evidence or {}))
        self.frame.show = True

    def continue_requested(self):
        return bool(self.beats)

    def finish(self, status):
        self.finished = status

    def supplied_answer(self):
        return self.answer


def _adapter() -> tuple[BrowserAdapter, list[str]]:
    notes: list[str] = []
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts=set()))
    adapter._say = notes.append  # type: ignore[method-assign]
    adapter._await_receipt = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return adapter, notes


def test_an_open_list_that_covers_the_field_is_chosen_instead_of_timing_out():
    """The second click opens the list and the list then blocks the click. Choose the row that matches the saved place."""

    class _Covered(_Field):
        def __init__(self, frame) -> None:
            super().__init__(frame)
            self.clicks = 0

        def click(self, timeout=None, force=False):
            self.clicks += 1
            if self.clicks >= 2:
                self.frame.show = True
                raise TimeoutError("covered")

    class _SavedFrame(_Frame):
        def locator(self, selector):
            if "option" in selector or "pac-item" in selector or "listbox" in selector:
                self.menu.rows = ["Lakeside, Harbor, Country", "Lakeside Beach, Harbor, Country"] if self.show else []
                return self.menu
            return _Empty()

    field = _Covered(None)
    frame = _SavedFrame(field)
    frame.show = False
    field.frame = frame
    scope = _Scope(field)
    adapter, notes = _adapter()
    step = Step("autocomplete", "#location", "Lakeside, Harbor", label="Location")
    evidence: dict = {"steps_done": 0, "final_clicked": False}
    plan = Plan(start_url="https://boards.example/apply", steps=[step], receipt_text="thank you")
    adapter._run_steps(scope, plan, evidence, adapter._shot_target(), None, None)
    assert field.typed == ["Lakeside, Harbor"]
    assert frame.menu.clicked == ["Lakeside, Harbor, Country"]
    assert evidence["location_chosen"] == "Lakeside, Harbor, Country"
    assert "Chose Lakeside, Harbor, Country." in " ".join(notes)


def test_a_menu_inside_the_fields_frame_picks_the_exact_city_row():
    field = _Field(None)
    frame = _Frame(field)
    field.frame = frame
    scope = _Scope(field, decoy=True)
    adapter, notes = _adapter()
    step = Step("autocomplete", "#location", "Lakeside, Harbor", label="Location")
    evidence: dict = {"steps_done": 0, "final_clicked": False}
    plan = Plan(start_url="https://boards.example/apply", steps=[step], receipt_text="thank you")
    adapter._run_steps(scope, plan, evidence, adapter._shot_target(), None, None)
    assert field.typed == ["Lakeside, Harbor", "Lakeside"]
    assert frame.menu.clicked == ["Lakeside, Harbor, Country"]
    assert scope.page.menu.clicked == []
    chosen = "Location: typed Lakeside. A list appeared. Chose Lakeside, Harbor, Country."
    assert chosen in notes
    assert "Location: no list appeared for Lakeside, Harbor" in notes
    assert "Location: no list appeared for Lakeside" not in notes
    assert evidence["location_typed"] == "Lakeside"
    assert evidence["location_chosen"] == "Lakeside, Harbor, Country"
    when = datetime(2026, 11, 6, tzinfo=UTC)
    app = {"status": "failed", "history": []}
    runs = [{"status": "completed", "events": [{"at": when, "message": chosen}]}]
    assert what_to_do_line(app, runs) == chosen


def test_an_optional_box_whose_list_never_matches_is_left_alone():
    """The synthetic Country box can land on another widget (a phone code picker). Optional means skip, not fail."""

    class _PhoneFrame(_Frame):
        def locator(self, selector):
            if "option" in selector and self.field.current == "Country":
                self.menu.rows = ["Country +1"]
                return self.menu
            return super().locator(selector)

    class _Clearable(_Field):
        def __init__(self, frame) -> None:
            super().__init__(frame)
            self.cleared = False

        def press(self, key, timeout=None):
            return None

        def fill(self, text, timeout=None):
            self.cleared = text == ""

    field = _Clearable(None)
    frame = _PhoneFrame(field)
    field.frame = frame
    adapter, notes = _adapter()
    step = Step("autocomplete", "#country", "Country", label="Country", optional=True)
    plan = Plan(start_url="https://boards.example/apply", steps=[step], receipt_text="thank you")
    evidence: dict = {"steps_done": 0, "final_clicked": False}
    adapter._run_steps(_Scope(field), plan, evidence, adapter._shot_target(), None, None)
    assert field.typed == ["Country"] and field.cleared and frame.menu.clicked == []
    assert "Country: no row matched, so it was left as it was. The field is optional." in notes
    assert evidence["steps_done"] == 1


def test_no_rows_still_asks_the_user():
    field = _Field(None)
    frame = _Frame(field)
    field.frame = frame
    frame.show = False
    hold = _Hold("West Harbor", frame)
    adapter, notes = _adapter()
    step = Step("autocomplete", "#location", "Lakeside, Northland", label="Location", fallbacks=["Harbor"])
    plan = Plan(start_url="https://boards.example/apply", steps=[step], receipt_text="thank you")
    evidence = {"steps_done": 0, "final_clicked": False}
    adapter._run_steps(_Scope(field), plan, evidence, adapter._shot_target(), None, hold)
    assert field.typed == ["Lakeside, Northland", "Lakeside", "Harbor", "West Harbor"]
    assert frame.menu.clicked == ["West Harbor, Harbor, Country"]
    assert "No list appeared" in " ".join(notes)
    assert "Location: no list appeared for Lakeside" in notes
    assert hold.beats and hold.beats[-1]["purpose"] == "field"
    assert "No list appeared" in hold.beats[-1]["field_reason"]
    assert "Location: typed West Harbor. A list appeared. Chose West Harbor, Harbor, Country." in notes


def test_a_later_place_is_reported_and_the_saved_place_is_not():
    assert location_choice_line("Lakeside, Northland", "Lakeside, Northland", "Lakeside, NO, Country") is None
    assert location_choice_line("Lakeside, Northland", "Harbor", "Harbor, NO, Country") == (
        "The location list did not offer the saved place, so the form used Harbor, NO, Country."
    )

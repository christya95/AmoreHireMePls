"""Fictional candidate preferences for tests.

The code ships with neutral, empty preferences; every rule a test relies on (role families, places, employers to skip,
the shape of a recruiter note, what counts as a skill word) is set here explicitly, the way a real candidate sets it in
``candidate/profile.toml``. Two personas: a backend developer in a fictional Ontario setup (the shape the suite grew up
with) and a sales account executive (proves nothing in the code assumes software).
"""

from __future__ import annotations

from amorehiremepls.discovery import FitTerms, fit_terms
from amorehiremepls.profile import ExcludedEmployer, FitRules, Preferences, Profile, ResumeRules, TermAlias, build_profile_from_skills

TECH_FIT = FitRules(
    skill_terms=[
        "java",
        "kotlin",
        "scala",
        "python",
        "golang",
        "typescript",
        "javascript",
        "node",
        "node.js",
        "spring",
        "hibernate",
        "rest",
        "restful",
        "api",
        "apis",
        "graphql",
        "grpc",
        "microservice",
        "microservices",
        "sql",
        "nosql",
        "postgres",
        "postgresql",
        "mysql",
        "oracle",
        "mongodb",
        "redis",
        "dynamodb",
        "kafka",
        "rabbitmq",
        "docker",
        "kubernetes",
        "k8s",
        "aws",
        "azure",
        "gcp",
        "jenkins",
        "junit",
        "pytest",
        "git",
        "linux",
        "terraform",
        "openshift",
        "ci/cd",
        "test",
        "tests",
        "testing",
    ],
    term_aliases=[
        TermAlias(words=["API", "APIs", "REST", "RESTful"], evidence=["API", "APIs", "REST", "RESTful"]),
        TermAlias(words=["database", "databases"], evidence=["database", "databases", "SQL", "PostgreSQL", "Postgres", "MySQL", "Oracle", "MongoDB", "Redis", "DynamoDB"]),
        TermAlias(words=["cloud"], evidence=["cloud", "AWS", "Azure", "GCP"]),
        TermAlias(words=["container", "containers", "containerization", "containerisation"], evidence=["container", "containers", "docker", "kubernetes", "k8s"]),
        TermAlias(words=["CI", "CD", "CI/CD"], evidence=["CI/CD", "CI", "GitHub Actions", "Jenkins", "GitLab", "pipeline", "pipelines"]),
        TermAlias(words=["test", "tests", "testing"], evidence=["test", "tests", "testing", "JUnit", "pytest", "TDD"]),
    ],
    hold_back_terms=["BMO"],
)


def tech_preferences() -> Preferences:
    """A fictional backend developer in Ontario: the rule set the original test suite was written against."""
    return Preferences(
        employment_types=["permanent"],
        role_families=["backend", "software developer", "software engineer", "java", "python", "platform", "api", "full stack", "ai engineer"],
        excluded_role_families=["frontend", "front-end", "ui developer", "mobile", "ios", "android", "qa", "test automation", "data scientist", "staff", "principal"],
        excluded_role_family_exceptions=["ai engineer"],
        locations=[
            "Ontario",
            "Toronto",
            "GTA",
            "Mississauga",
            "Milton",
            "Oakville",
            "Markham",
            "Vaughan",
            "Brampton",
            "Burlington",
            "Hamilton",
            "Waterloo",
            "Kitchener",
            "Ottawa",
            "Canada",
        ],
        remote_eligible_terms=["ontario", "canada", "canadian residents"],
        region_label="Ontario/Canada",
        remote_rule="Remote roles must be open to Ontario residents.",
        excluded_employers=[
            ExcludedEmployer(name="RBC", aliases=["Royal Bank of Canada"], reason="fixture"),
            ExcludedEmployer(name="Meta", aliases=["Facebook"], reason="fixture"),
            ExcludedEmployer(name="Apple", reason="fixture"),
            ExcludedEmployer(name="Amazon", aliases=["AWS", "Amazon Web Services"], reason="fixture"),
            ExcludedEmployer(name="Netflix", reason="fixture"),
            ExcludedEmployer(name="Google", aliases=["Alphabet"], reason="fixture"),
        ],
        small_startup_rule="needs_review",
        startup_signals_extra=["founding engineer", "first engineer"],
        work_authorization="Authorized to work in Canada without sponsorship",
        outreach_rules={
            "connect_note_max_chars": 300,
            "no_dashes": True,
            "no_layoff_mention": True,
            "mention_applied_role": True,
            "ask_other_roles": True,
            "role_phrase": "permanent backend roles",
            "one_liner": "I build backend services in Java and Python",
            "message_words": [45, 75],
            "draft_by_default": True,
        },
        resume=ResumeRules(max_pages=2, allowed_terms=["Backend", "Software", "Developer", "Engineer", "Toronto", "Ontario", "Canada", "Java", "Python"]),
        fit=TECH_FIT,
    )


TECH_RULES = tech_preferences().outreach_rules


def sales_preferences() -> Preferences:
    """A fictional enterprise account executive in the UK."""
    return Preferences(
        employment_types=["permanent"],
        role_families=["account executive", "account manager", "sales executive", "business development", "enterprise sales"],
        excluded_role_families=["sdr", "bdr", "inside sales", "retail", "call centre"],
        locations=["London", "Manchester", "United Kingdom", "UK"],
        remote_eligible_terms=["uk", "united kingdom"],
        region_label="the UK",
        excluded_employers=[ExcludedEmployer(name="Initech", aliases=["Initech Group"], reason="fixture")],
        small_startup_rule="ignore",
        startup_signals_extra=["first sales hire"],
        work_authorization="Right to work in the UK",
        outreach_rules={
            "connect_note_max_chars": 300,
            "no_dashes": True,
            "no_layoff_mention": True,
            "mention_applied_role": True,
            "ask_other_roles": True,
            "role_phrase": "permanent enterprise sales roles",
            "one_liner": "I sell data platforms to mid market finance teams",
        },
        resume=ResumeRules(max_pages=1, section_aliases={"skills": ["Toolbox"]}, allowed_terms=["London"]),
        fit=FitRules(
            skill_terms=["salesforce", "hubspot", "outreach", "gong", "meddic", "meddpicc", "spin", "challenger", "forecasting", "pipeline", "quota", "arr", "upsell", "renewals"],
            term_aliases=[TermAlias(words=["CRM"], evidence=["CRM", "Salesforce", "HubSpot"])],
            hold_back_terms=["Initech"],
        ),
    )


def build_test_profile(facts_text: str | None, salary_text: str | None, confirmed_on: str | None = None, apply_text: str | None = None, overrides: dict | None = None) -> Profile:
    """``build_profile_from_skills`` with the fictional backend developer's preferences applied."""
    profile = build_profile_from_skills(facts_text, salary_text, confirmed_on, apply_text=apply_text, overrides=overrides, preferences=tech_preferences())
    profile.candidate.profession = "backend software developer"
    if not profile.compensation.currency:
        profile.compensation.currency = "CAD"
    return profile


def tech_fit_terms(evidence_skills: list[str] | None = None) -> FitTerms:
    prefs = tech_preferences()
    return fit_terms(TECH_FIT, evidence_skills, [*prefs.role_families, "backend software developer"])

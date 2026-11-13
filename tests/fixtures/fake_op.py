"""An in-memory stand-in for the 1Password command line tool, for tests. Nothing here is a real vault, item or login.

It is passed to `OpCliSource(binary="python3", run=FakeOp())`. It keeps items in a dict, records every call with its
arguments and what was piped to it, and understands only the commands the app sends: item list, get, create from a
piped template, edit with assignments, and delete or archive.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

VAULT = {"id": "vault-fixture-1", "name": "Fixture Vault"}
_ASSIGN = re.compile(r"^(?:(?P<section>[^.\[=]+)\.)?(?P<field>[^.\[=]+)(?:\[(?P<kind>[a-z]+)\])?=(?P<value>.*)$", re.S)


def generated(recipe: str, number: int) -> str:
    """A deterministic fictional password that follows a recipe such as `letters,digits,symbols,32`."""
    parts = recipe.split(",")
    length = int(next(part for part in parts if part.isdigit()))
    kinds = ["a", "Q"] + (["7"] if "digits" in parts else []) + (["!", "_"] if "symbols" in parts else [])
    return "".join(kinds[(index + number) % len(kinds)] for index in range(length))


class FakeOp:
    def __init__(self, passwords: list[str] | None = None, vaults: tuple[str, ...] = (VAULT["name"], VAULT["id"]), echo_password: bool = True) -> None:
        self.items: dict[str, dict] = {}
        self.archived: list[str] = []
        self.deleted: list[str] = []
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail: list[str | Exception] = []  # the next calls fail with this error text, or raise this
        self.passwords = list(passwords or [])
        self.vaults = vaults
        self.echo_password = echo_password  # False: a create or an edit answers without the password value
        self._made = 0

    # -- seeding and reading in tests -----------------------------------------------------------------------
    def add_login(
        self, ref: str, title: str, hosts: tuple[str, ...], username: str = "", password: str = "seeded-pass-1", tags: tuple[str, ...] = (), extra: list[dict] | None = None
    ) -> dict:
        item = {
            "id": ref,
            "title": title,
            "category": "LOGIN",
            "tags": list(tags),
            "version": 1,
            "vault": dict(VAULT),
            "updated_at": "2026-01-01T00:00:00Z",
            "urls": [{"href": f"https://{host}/"} for host in hosts],
            "sections": [],
            "fields": [
                {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": username},
                {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": password},
                *(extra or []),
            ],
        }
        self.items[ref] = item
        return item

    def password_of(self, ref: str) -> str:
        return next(row["value"] for row in self.items[ref]["fields"] if row.get("purpose") == "PASSWORD")

    def field(self, ref: str, label: str) -> str:
        return next((row.get("value", "") for row in self.items[ref]["fields"] if row.get("label") == label), "")

    def verbs(self) -> list[str]:
        return [" ".join(args[1:3]) for args, _ in self.calls]

    # -- the tool -------------------------------------------------------------------------------------------
    def __call__(self, command, capture_output=True, text=True, timeout=None, stdin=None, input=None):  # noqa: A002
        args = list(command)
        self.calls.append((args, input))
        if self.fail:
            failure = self.fail.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return SimpleNamespace(returncode=1, stdout="", stderr=failure)
        flags, rest = self._split(args[1:])
        if "vault" in flags and flags["vault"] not in self.vaults:
            return SimpleNamespace(returncode=1, stdout="", stderr=f'[ERROR] "{flags["vault"]}" isn\'t a vault in this account. Specify the vault with its ID or name.')
        try:
            out = getattr(self, f"_{rest[0]}_{rest[1]}")(rest[2:], flags, input)
        except KeyError:
            return SimpleNamespace(returncode=1, stdout="", stderr="[ERROR] item not found")
        return SimpleNamespace(returncode=0, stdout=out if isinstance(out, str) else json.dumps(out), stderr="")

    def _split(self, args: list[str]) -> tuple[dict, list[str]]:
        flags: dict[str, str] = {}
        rest: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg.startswith("--"):
                name, eq, value = arg[2:].partition("=")
                if eq or name in ("archive", "otp", "dry-run", "reveal"):
                    flags[name] = value if eq else "true"
                else:
                    flags[name] = args[index + 1]
                    index += 1
            else:
                rest.append(arg)
            index += 1
        return flags, rest

    def _new_password(self, recipe: str) -> str:
        self._made += 1
        return self.passwords.pop(0) if self.passwords else generated(recipe, self._made)

    def _shown(self, item: dict) -> dict:
        if self.echo_password:
            return item
        return {**item, "fields": [{k: v for k, v in row.items() if not (k == "value" and row.get("purpose") == "PASSWORD")} for row in item["fields"]]}

    def _item_list(self, rest: list[str], flags: dict, piped: str | None) -> list[dict]:
        rows = []
        for item in self.items.values():
            if flags.get("categories") and item["category"].lower() != flags["categories"].lower().replace(" ", "_"):
                continue
            if flags.get("tags") and not set(flags["tags"].split(",")) & set(item["tags"]):
                continue
            username = next((row.get("value", "") for row in item["fields"] if row.get("id") == "username"), "")
            rows.append({key: item[key] for key in ("id", "title", "tags", "version", "vault", "category", "urls", "updated_at")} | {"additional_information": username})
        return rows

    def _item_get(self, rest: list[str], flags: dict, piped: str | None) -> dict:
        return self.items[rest[0]]

    def _item_create(self, rest: list[str], flags: dict, piped: str | None) -> dict:
        assert rest and rest[0] == "-" and piped, "the item must arrive as a template on standard input"
        template = json.loads(piped)
        sections = {row["id"]: row for row in template.get("sections", [])}
        ref = f"item-fixture-{len(self.items) + 1}"
        item = {
            "id": ref,
            "title": template["title"],
            "category": template["category"],
            "tags": sorted(template.get("tags", [])),
            "version": 1,
            "vault": dict(VAULT),
            "updated_at": f"2026-01-02T00:00:{len(self.items):02d}Z",
            "urls": template.get("urls", []),
            "sections": list(sections.values()),
            "fields": [{**row, **({"section": sections[row["section"]["id"]]} if row.get("section") else {})} for row in template["fields"]],
        }
        if "generate-password" in flags:
            next(row for row in item["fields"] if row.get("purpose") == "PASSWORD")["value"] = self._new_password(flags["generate-password"])
        self.items[ref] = item
        return self._shown(item)

    def _item_edit(self, rest: list[str], flags: dict, piped: str | None) -> dict:
        item = self.items[rest[0]]
        for assignment in rest[1:]:
            found = _ASSIGN.match(assignment)
            assert found, assignment
            section, label, value = found.group("section"), found.group("field"), found.group("value")
            row = next((r for r in item["fields"] if r.get("label") == label and (not section or (r.get("section") or {}).get("label") == section)), None)
            if row is None:
                box = next((s for s in item["sections"] if s["label"] == section), None)
                if section and box is None:
                    box = {"id": section.lower().replace(" ", "_"), "label": section}
                    item["sections"].append(box)
                row = {"id": label.lower().replace(" ", "_"), "type": "STRING", "label": label, **({"section": box} if box else {})}
                item["fields"].append(row)
            row["value"] = value
        if "generate-password" in flags:
            next(row for row in item["fields"] if row.get("purpose") == "PASSWORD")["value"] = self._new_password(flags["generate-password"])
        if "tags" in flags:
            item["tags"] = sorted(tag for tag in flags["tags"].split(",") if tag)
        if "title" in flags:
            item["title"] = flags["title"]
        item["version"] += 1
        item["updated_at"] = f"2026-01-03T00:00:{item['version']:02d}Z"
        return self._shown(item)

    def _item_delete(self, rest: list[str], flags: dict, piped: str | None) -> str:
        item = self.items.pop(rest[0])
        (self.archived if "archive" in flags else self.deleted).append(item["id"])
        return ""

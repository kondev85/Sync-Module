"""Offline Pipeline matching, write-boundary, caching, and note-idempotency tests."""

from collections import Counter
from contextlib import redirect_stdout
import io
import os
import unittest
from unittest.mock import Mock, patch

import sigma_sync
from sigma_sync import pipeline, importer, notion_import


def title(content):
    return {"type": "title", "title": [{"plain_text": content, "type": "text", "text": {"content": content}}]}


def company(pid, name):
    return {"id": pid, "properties": {"Company": title(name)}}


def person(pid, name, linkedin="", companies=("company",), tags=()):
    return {"id": pid, "properties": {
        "Name": title(name), "LinkedIn": {"type": "url", "url": linkedin or None},
        "Company": {"type": "relation", "relation": [{"id": c} for c in companies]},
        "Source": {"type": "multi_select", "multi_select": list(tags)},
    }}


def contact(**changes):
    return {"first_name": "Oliver", "last_name": "M.", "company": "Playtech", "linkedin": "", **changes}


CONTACTS_DB = {"id": "contacts", "properties": {
    "Name": {"type": "title"}, "LinkedIn": {"type": "url"},
    "Company": {"type": "relation", "relation": {"database_id": "companies"}},
    "Source": {"type": "multi_select", "multi_select": {"options": [{"name": "SiGMA2026"}]}},
}}
COMPANIES_DB = {"id": "companies", "properties": {"Company": {"type": "title"}}}


class PipelineTests(unittest.TestCase):
    def load(self, contacts, companies=None):
        with patch.object(pipeline, "discover_databases", return_value=(CONTACTS_DB, COMPANIES_DB)), \
             patch.object(pipeline, "_paginate", side_effect=[
                 companies or [company("company", "Playtech plc")], contacts
             ]) as paginate, redirect_stdout(io.StringIO()):
            checker = pipeline.PipelineCheck()
        self.assertEqual(paginate.call_count, 2)
        return checker

    def test_same_linkedin_takes_priority_over_name_and_company(self):
        checker = self.load([
            person("linkedin", "Different Name", "https://www.linkedin.com/in/OLIVER/"),
            person("name", "Oliver Marshall"),
        ])
        self.assertEqual(checker.match(contact(linkedin="http://linkedin.com/in/oliver?trk=123")), ["linkedin"])

    def test_same_first_name_initial_and_related_company_title(self):
        checker = self.load([person("name", "Oliver Marshall")])
        self.assertEqual(checker.match(contact(first_name="oLiVeR", company="Playtech, LLC")), ["name"])
        self.assertEqual(checker.match(contact(last_name="N.")), [])
        self.assertEqual(checker.match(contact(company="Other Company")), [])

    @patch.object(pipeline, "_request")
    def test_two_name_matches_are_ambiguous_and_never_tagged(self, request):
        checker = self.load([person("one", "Oliver Marshall"), person("two", "Oliver Matthews")])
        output = io.StringIO()
        with redirect_stdout(output):
            status, name = checker.check(contact())
        self.assertEqual(status, "pipeline ambiguous")
        self.assertIsNone(name)
        self.assertIn("pipeline ambiguous: Oliver Marshall; Oliver Matthews", output.getvalue())
        self.assertEqual(checker.counts["pipeline ambiguous"], 1)
        request.assert_not_called()

    @patch.object(pipeline, "_request")
    def test_ambiguous_linkedin_does_not_fall_back_to_unique_name(self, request):
        checker = self.load([
            person("one", "Oliver Marshall", "https://linkedin.com/in/duplicate"),
            person("two", "Someone Else", "https://www.linkedin.com/in/duplicate/"),
        ])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(checker.check(contact(linkedin="https://linkedin.com/in/duplicate"))[0], "pipeline ambiguous")
        request.assert_not_called()

    @patch.object(pipeline, "_request")
    def test_tag_already_present_makes_no_write(self, request):
        checker = self.load([person("one", "Oliver Marshall", tags=[
            {"id": "old", "name": "Existing"}, {"id": "sigma", "name": "SiGMA2026"}
        ])])
        self.assertEqual(checker.check(contact()), ("pipeline already tagged", "Oliver Marshall"))
        self.assertEqual(checker.counts["pipeline already tagged"], 1)
        request.assert_not_called()

    @patch.object(pipeline.time, "sleep")
    @patch.object(pipeline, "_request")
    def test_tag_write_preserves_options_and_changes_only_source(self, request, sleep):
        checker = self.load([person("one", "Oliver Marshall", tags=[{"id": "old", "name": "Existing"}])])
        self.assertEqual(checker.check(contact())[0], "pipeline tagged")
        request.assert_called_once_with("PATCH", "/pages/one", {
            "properties": {"Source": {"multi_select": [{"id": "old"}, {"name": "SiGMA2026"}]}}
        })
        self.assertEqual(checker.counts["pipeline tagged"], 1)
        self.assertEqual(checker.check(contact())[0], "pipeline already tagged")
        self.assertEqual(request.call_count, 1)  # No per-attendee GET, query, or second write.
        self.assertEqual(checker.contacts["one"]["properties"]["Name"], title("Oliver Marshall"))

    def test_multiple_company_relations_do_not_duplicate_same_person(self):
        checker = self.load([person("one", "Oliver Marshall", companies=("a", "b"))],
                            [company("a", "Playtech plc"), company("b", "Playtech Ltd")])
        self.assertEqual(checker.match(contact()), ["one"])

    @patch.object(pipeline, "_request")
    def test_no_match_does_nothing(self, request):
        checker = self.load([person("one", "Other Name")])
        self.assertEqual(checker.check(contact()), ("pipeline not found", None))
        request.assert_not_called()
        self.assertEqual(sum(checker.counts.values()), 0)

    @patch.object(pipeline.time, "sleep")
    @patch.object(pipeline, "_request")
    def test_startup_pagination_loads_all_pages(self, request, sleep):
        request.side_effect = [
            {"results": [{"id": "a"}], "has_more": True, "next_cursor": "cursor"},
            {"results": [{"id": "b"}], "has_more": False},
        ]
        self.assertEqual([p["id"] for p in pipeline._paginate("/databases/contacts/query")], ["a", "b"])
        self.assertEqual(request.call_args.args[2]["start_cursor"], "cursor")


class LeadNoteTests(unittest.TestCase):
    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_note_append_preserves_existing_rich_text_and_other_fields(self, request, sleep):
        checker = notion_import.NotionImporter.__new__(notion_import.NotionImporter)
        checker.schema, checker.pages = {"Notes": "rich_text"}, {}
        original = {"type": "text", "text": {"content": "Keep this", "link": None},
                    "plain_text": "Keep this", "href": None, "annotations": {"bold": True}}
        page = {"id": "lead", "properties": {"Notes": {"type": "rich_text", "rich_text": [original]}}}
        first = Mock()
        first.json.return_value = page
        request.return_value = first
        self.assertTrue(checker.add_pipeline_note("lead", "Oliver Marshall"))
        payload = request.call_args.args[2]
        self.assertEqual(set(payload["properties"]), {"Notes"})
        parts = payload["properties"]["Notes"]["rich_text"]
        self.assertEqual(parts[0]["text"]["content"], "Keep this")
        self.assertTrue(parts[0]["annotations"]["bold"])
        self.assertEqual(parts[1]["text"]["content"], "\n\nIn Pipeline: Oliver Marshall")
        self.assertNotIn("plain_text", parts[0])
        page["properties"]["Notes"]["rich_text"] = parts
        request.reset_mock()
        self.assertFalse(checker.add_pipeline_note("lead", "Oliver Marshall"))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")


class PipelineRunnerTests(unittest.TestCase):
    @patch.dict(os.environ, {"SIGMA_COOKIE": "fake", "NOTION_API_TOKEN": "fake",
                            "NOTION_DATABASE_ID": "fake", "MAX_CONTACTS": "1",
                            "SIGMA_BUSINESS_TYPES": "", "SIGMA_PIPELINE_CHECK": "1"})
    @patch.object(importer.config, "SKIP_CONTACTS", 0)
    @patch.object(importer, "PipelineCheck")
    @patch.object(importer, "NotionImporter")
    @patch.object(importer, "SigmaClient")
    def test_pipeline_also_runs_for_already_imported_attendees(self, source, notion, pipeline_cls):
        attendee_id = "11111111-1111-4111-8111-111111111111"
        source.return_value.attendees.return_value = ([{
            "id": attendee_id, "first_name": "Oliver", "last_name": "M.", "company": "Playtech",
        }], 1)
        source.return_value.profile.return_value = {"bio": "", "linkedin": "https://linkedin.com/in/oliver"}
        notion.return_value.schema = {"Notes": "rich_text"}
        notion.return_value.already_imported.return_value = True
        notion.return_value.last_page_id = "lead"
        notion.return_value.sync.return_value = ("skipped", "SIGMA URL already imported")
        pipeline_cls.return_value.check.return_value = ("pipeline already tagged", "Oliver Marshall")
        pipeline_cls.return_value.counts = Counter({"pipeline already tagged": 1})
        output = io.StringIO()
        with redirect_stdout(output):
            importer.run()
        pipeline_cls.assert_called_once()
        pipeline_cls.return_value.check.assert_called_once()
        notion.return_value.add_pipeline_note.assert_called_once_with("lead", "Oliver Marshall")
        self.assertIn("pipeline already tagged", output.getvalue())
        source.return_value.profile.assert_called_once_with(attendee_id)


if __name__ == "__main__":
    unittest.main()

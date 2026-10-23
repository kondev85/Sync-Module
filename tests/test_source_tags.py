"""Offline Source1 attribution regressions for both importers."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

import sigma_sync
import config
import notion_sync
from sigma_sync import notion_import
from tests import test_sigma as fixtures
from tests.test_sigma import contact, page, response


class SourceTagTests(unittest.TestCase):
    def test_both_importers_merge_tags_and_never_write_checkboxes(self):
        schema = {**notion_sync.EXPECTED_TYPES, "SiGMAWorld": "checkbox", "iGBLive": "checkbox"}
        for tag, build in [
            ("IGB2026", lambda old: notion_sync.build_properties(contact(), old)),
            ("SiGMA2026", lambda old: notion_import.build_properties(contact(), schema, old)),
        ]:
            with self.subTest(tag=tag), patch.object(notion_sync, "get_schema", return_value=schema):
                new = build(None)
                self.assertEqual(new["Source1"]["multi_select"], [{"name": tag}])
                existing = page("one", Source1={"multi_select": [
                    {"id": "keep", "name": "Existing", "color": "blue"},
                    {"name": "Other"},
                ]})
                updated = build(existing)
                self.assertEqual(updated["Source1"]["multi_select"],
                                 [{"id": "keep"}, {"name": "Other"}, {"name": tag}])
                existing["properties"]["Source1"]["multi_select"].append({"id": "tag", "name": tag})
                self.assertNotIn("Source1", build(existing))
                for payload in (new, updated, build(existing)):
                    self.assertNotIn("iGBLive", payload)
                    self.assertNotIn("SiGMAWorld", payload)

    def test_missing_or_wrong_source_type_warns_and_skips_without_failure(self):
        for actual in (None, "rich_text", "select"):
            schema = {"Name": "title"}
            if actual:
                schema["Source1"] = actual
            with self.subTest(actual=actual), \
                 patch.object(notion_sync, "get_schema", return_value=schema), \
                 patch.object(notion_import.NotionImporter, "_load"):
                output = io.StringIO()
                with redirect_stdout(output):
                    notion_sync.report_schema_mismatches()
                    notion_import.NotionImporter()
                self.assertIn("WARNING", output.getvalue())
                self.assertIn("Source1", output.getvalue())
                self.assertNotIn("Source1", notion_sync.build_properties(contact()))
                self.assertNotIn("Source1", notion_import.build_properties(contact(), schema))

    @patch.object(notion_sync, "_notion_request")
    @patch.object(notion_sync, "get_schema", return_value=notion_sync.EXPECTED_TYPES)
    def test_swapcard_update_refreshes_existing_sources(self, schema, request):
        existing = page("one", Source1={"multi_select": [{"id": "old", "name": "SiGMA2026"}]})
        request.side_effect = [response(existing), response(existing)]
        notion_sync.update_contact("one", contact())
        self.assertEqual(request.call_args_list[0].args[0], "GET")
        self.assertEqual(request.call_args.args[2]["properties"]["Source1"]["multi_select"],
                         [{"id": "old"}, {"name": "IGB2026"}])

    @patch.object(notion_sync, "_notion_request")
    @patch.object(notion_sync, "get_schema", return_value=notion_sync.EXPECTED_TYPES)
    def test_swapcard_create_adds_source(self, schema, request):
        request.return_value = response(page("new"))
        notion_sync.create_contact(contact())
        self.assertEqual(request.call_args.args[2]["properties"]["Source1"]["multi_select"],
                         [{"name": "IGB2026"}])

    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_sync, "_notion_request")
    def test_sigma_counts_only_successful_missing_tags(self, request, sleep):
        for tagged in (False, True):
            existing = page("one", Source1={"multi_select": [{"name": "SiGMA2026"}] if tagged else []})
            obj = fixtures.DedupTests().make_importer([existing])
            request.side_effect = [response(existing), response(existing)]
            obj.sync(contact())
            self.assertEqual(obj.source_tags_added, 0 if tagged else 1)
        obj = fixtures.DedupTests().make_importer([])
        request.side_effect = [response(page("new"))]
        obj.sync(contact())
        self.assertEqual(obj.source_tags_added, 1)
        obj = fixtures.DedupTests().make_importer([])
        failed = response()
        failed.raise_for_status.side_effect = RuntimeError("write failed")
        request.side_effect = [failed]
        with self.assertRaises(RuntimeError):
            obj.sync(contact())
        self.assertEqual(obj.source_tags_added, 0)


if __name__ == "__main__":
    unittest.main()

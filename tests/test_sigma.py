"""Offline safety/regression tests. Never use live SiGMA or Notion credentials."""

from contextlib import redirect_stdout
from html import escape
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import sigma_sync
from sigma_sync import client, importer, notion_import, profiles

ID = "11111111-1111-4111-8111-111111111111"
URL = f"https://match.sigma.world/attendees/{ID}"
SCHEMA = {name: allowed[0] for name, (_, allowed) in notion_import.FIELDS.items()}


def page(pid, name="Oliver Marshall", company="Playtech plc", **fields):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
        "Company": {"type": "rich_text", "rich_text": [{"plain_text": company}]},
    }
    props.update(fields)
    return {"id": pid, "properties": props}


def response(data=None, code=200, headers=None, body=None):
    result = Mock(status_code=code, headers=headers or {}, is_redirect=300 <= code < 400)
    result.text = body or json.dumps(data or {})
    result.json.return_value = data or {}
    return result


def contact(**fields):
    return {
        "name": "Oliver M.", "first_name": "Oliver", "last_name": "M.",
        "company": "Playtech", "role": "Director", "linkedin": "",
        "sigma_url": URL, "business_type": ["Operator"], "notes": "New bio",
        **fields,
    }


class ParsingTests(unittest.TestCase):
    def test_json_and_inertia_exclude_session_metadata(self):
        data = {"props": {"auth": {"user": {"bio": "WRONG"}}, "attendee": {"id": ID, "bio": "RIGHT"}}}
        self.assertEqual(profiles.parse_profile(json.dumps(data))["bio"], "RIGHT")
        body = '<div data-page="' + escape(json.dumps(data), quote=True) + '"></div>'
        self.assertEqual(profiles.parse_profile(body)["bio"], "RIGHT")

    def test_generic_html_sections_and_links(self):
        html = """<h2>Bio</h2><p>Hall 8 | Booth 8110. Telegram @example</p>
        <h2>What I Provide</h2><p>Payments</p>
        <h2>I'm Looking For</h2><p>Operators</p><h2>Languages</h2><p>English</p>
        <a href="https://linkedin.com/in/example">LinkedIn</a>
        <a href="https://example.com">Website</a>"""
        result = profiles.parse_profile(html)
        self.assertIn("Hall 8", result["bio"])
        self.assertEqual(result["provide"], "Payments")
        self.assertEqual(result["linkedin"], "https://linkedin.com/in/example")
        self.assertEqual(result["website"], "https://example.com")

    def test_notes_mapping_and_email_only_from_bio(self):
        item = {"id": ID, "first_name": "Oliver", "last_name": "M.", "company": "Playtech", "job_title": "Director"}
        profile = {
            "bio": "<p>Hall 8 | Booth 8110. Telegram @sample. Reach lead@example.org</p>",
            "business_type": ["Operator"], "vertical": ["Supplier"],
            "product_service_categories_provide": ["Payments"],
            "product_service_categories_looking_for": ["CRM"],
            "continents_of_operation": ["Europe"], "continents_of_interest": ["Asia"],
            "languages": ["English"], "website": "example.org", "email": "private@example.org",
        }
        mapped = profiles.build_contact(item, profile)
        self.assertEqual(mapped["email"], "lead@example.org")
        self.assertEqual(mapped["website"], "https://example.org")
        for snippet in ["Booth 8110", "@sample", "What I Provide: Operator", "I'm Looking For: Supplier",
                        "Payments", "CRM", "Europe", "Asia", "English"]:
            self.assertIn(snippet, mapped["notes"])
        profile["bio"] = "No public email"
        self.assertEqual(profiles.build_contact(item, profile)["email"], "")

    def test_unrecognized_profile_is_not_imported(self):
        for body in ["{}", "<html>Login required</html>", '{"props":{"auth":{"user":{"bio":"private"}}}}']:
            with self.assertRaises(ValueError):
                profiles.parse_profile(body)

    def test_urls_reject_non_web_schemes(self):
        self.assertEqual(profiles.clean_url("javascript:alert(1)"), "")
        self.assertEqual(profiles.clean_url("mailto:x@example.com"), "")


class DedupTests(unittest.TestCase):
    def make_importer(self, pages):
        obj = notion_import.NotionImporter.__new__(notion_import.NotionImporter)
        obj.schema = SCHEMA
        obj.pages = {p["id"]: p for p in pages}
        obj._index()
        return obj

    def test_company_normalization_and_initial(self):
        self.assertEqual(notion_import.name_key("oLIVER", "M.", "Playtech"),
                         notion_import.name_key("Oliver", "Marshall", "Playtech plc"))
        self.assertIsNone(notion_import.name_key("Oliver", "", "Playtech"))
        self.assertNotEqual(notion_import.name_key("Oliver", "M.", "Playtech"),
                            notion_import.name_key("Oliver", "N.", "Playtech"))

    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_sigma_url_has_priority_and_skips(self, request):
        obj = self.make_importer([page("one", **{"SIGMA URL": {"url": URL}})])
        self.assertEqual(obj.sync(contact())[0], "skipped")
        request.assert_not_called()

    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_linkedin_match_has_priority_over_name(self, request, sleep):
        first = page("linkedin", "Someone Else", "Other", LinkedIn={"url": "https://www.linkedin.com/in/oliver/"})
        second = page("name")
        obj = self.make_importer([first, second])
        request.side_effect = [response(first), response(first)]
        self.assertEqual(obj.sync(contact(linkedin="http://linkedin.com/in/oliver?trk=x"))[0], "updated")
        self.assertTrue(request.call_args_list[1].args[1].endswith("/linkedin"))

    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_unique_name_match_keeps_filled_fields(self, request, sleep):
        existing = page("one", Notes={"rich_text": [{"plain_text": "Keep my notes"}]},
                        Role={"rich_text": [{"plain_text": "CEO"}]}, Status={"select": {"name": "Qualified"}})
        obj = self.make_importer([existing])
        request.side_effect = [response(existing), response(existing)]
        self.assertEqual(obj.sync(contact())[0], "updated")
        properties = request.call_args_list[1].args[2]["properties"]
        for key in ("Name", "Company", "Notes", "Role", "Status"):
            self.assertNotIn(key, properties)
        self.assertTrue(properties["SiGMAWorld"]["checkbox"])
        self.assertEqual(properties["SIGMA URL"]["url"], URL)

    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_ambiguous_name_creates_flagged_row(self, request, sleep):
        obj = self.make_importer([page("one"), page("two", "Oliver Matthews")])
        request.return_value = response(page("new", "Oliver M."))
        status, _ = obj.sync(contact())
        self.assertEqual(status, "check-duplicate")
        call = request.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertFalse(call.kwargs["idempotent"])
        properties = call.args[2]["properties"]
        self.assertEqual(properties["Status"]["select"]["name"], "Check duplicate")
        self.assertIn("Oliver Matthews", properties["Notes"]["rich_text"][0]["text"]["content"])

    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_new_row_status_and_same_run_index(self, request, sleep):
        obj = self.make_importer([])
        saved = page("new", "Oliver M.", **{"SIGMA URL": {"url": URL}})
        request.return_value = response(saved)
        self.assertEqual(obj.sync(contact())[0], "created")
        self.assertEqual(request.call_args.args[2]["properties"]["Status"]["select"]["name"], "New")
        self.assertEqual(obj.sync(contact())[0], "skipped")
        self.assertEqual(request.call_count, 1)

    def test_missing_columns_and_long_notes(self):
        properties = notion_import.build_properties(contact(notes="A" * 5001), {"Name": "title", "Notes": "rich_text"})
        self.assertEqual(set(properties), {"Name", "Notes"})
        self.assertEqual([len(p["text"]["content"]) for p in properties["Notes"]["rich_text"]], [2000, 2000, 1001])

    def test_all_status_types(self):
        for kind in ["select", "status", "multi_select", "rich_text"]:
            p = notion_import.build_properties(contact(status="New"), {"Status": kind})
            self.assertIn(kind, p["Status"])
        p = notion_import.build_properties(contact(status="New"), {"Status": "multi_select"})
        self.assertEqual(p["Status"]["multi_select"], [{"name": "New"}])

    def test_sigma_url_supports_both_spellings_and_prefers_lead_pool(self):
        for schema, target in [
            ({"SIGMA URL": "url"}, "SIGMA URL"),
            ({"SiGMA URL": "url"}, "SiGMA URL"),
            ({"SIGMA URL": "url", "SiGMA URL": "url"}, "SIGMA URL"),
        ]:
            result = notion_import.build_properties(contact(), schema)
            self.assertEqual(result, {target: {"url": URL}})
        obj = self.make_importer([page("one", **{"SiGMA URL": {"url": URL}})])
        self.assertTrue(obj.already_imported(URL))

    @patch.object(notion_import.time, "sleep")
    @patch.object(notion_import.notion_sync, "get_schema", return_value=SCHEMA)
    @patch.object(notion_import.notion_sync, "ensure_required_schema")
    @patch.object(notion_import.notion_sync, "_notion_request")
    def test_all_notion_pages_are_loaded(self, request, ensure, schema, sleep):
        request.side_effect = [
            response({"results": [page("first")], "has_more": True, "next_cursor": "next"}),
            response({"results": [page("second")], "has_more": False}),
        ]
        with redirect_stdout(io.StringIO()):
            obj = notion_import.NotionImporter()
        self.assertEqual(len(obj.pages), 2)
        self.assertEqual(request.call_args.args[2]["start_cursor"], "next")


class ClientTests(unittest.TestCase):
    @patch.object(client.time, "sleep")
    def test_expired_sessions_stop(self, sleep):
        for reply in [response(code=401), response(code=419),
                      response(code=302, headers={"Location": "/login"}),
                      response(headers={"Content-Type": "text/html"}, body='<input type="password">')]:
            c = client.SigmaClient("XSRF-TOKEN=abc%3D; session=fake")
            with patch.object(c.session, "get", return_value=reply) as request:
                with self.assertRaises(client.CookieExpired):
                    c.get("/attendees?page=1")
                self.assertEqual(request.call_count, 1)

    @patch.object(client.time, "sleep")
    def test_xsrf_decoding_read_only_and_retry(self, sleep):
        c = client.SigmaClient("XSRF-TOKEN=abc%3D; session=fake")
        self.assertEqual(c._xsrf_header()["X-XSRF-TOKEN"], "abc=")
        with patch.object(c.session, "get", side_effect=[
            response(code=429, headers={"Retry-After": "1"}), response({"ok": True})
        ]) as request:
            c.get("/attendees?page=1")
        self.assertEqual(request.call_count, 2)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        with self.assertRaises(ValueError):
            c.get("https://example.org/attendees")


class RunnerTests(unittest.TestCase):
    @patch.dict(os.environ, {"SIGMA_COOKIE": "fake", "NOTION_API_TOKEN": "fake",
                            "NOTION_DATABASE_ID": "fake", "MAX_CONTACTS": "2",
                            "SIGMA_BUSINESS_TYPES": "Operator"})
    @patch.object(importer.config, "SKIP_CONTACTS", 0)
    @patch.object(importer, "NotionImporter")
    @patch.object(importer, "SigmaClient")
    def test_filter_before_profiles_and_cap(self, source, notion):
        client_obj = source.return_value
        client_obj.attendees.return_value = ([
            {"id": str(i).zfill(8) + "-1111-4111-8111-111111111111",
             "first_name": "Test", "last_name": "A.", "company": "Example",
             "business_type": [kind]} for i, kind in enumerate(["Supplier", "Operator", "Operator", "Operator"])
        ], 100)
        client_obj.profile.return_value = {"bio": "Example"}
        notion.return_value.already_imported.return_value = False
        notion.return_value.sync.return_value = ("created", "")
        with redirect_stdout(io.StringIO()):
            importer.run()
        self.assertEqual(client_obj.profile.call_count, 2)
        self.assertEqual(notion.return_value.sync.call_count, 2)
        self.assertEqual(client_obj.attendees.call_count, 1)
        client_obj.close.assert_called_once()

    @patch.dict(os.environ, {"SIGMA_COOKIE": "fake", "NOTION_API_TOKEN": "fake",
                            "NOTION_DATABASE_ID": "fake", "MAX_CONTACTS": "1"})
    @patch.object(importer.config, "SKIP_CONTACTS", 20)
    @patch.object(importer, "NotionImporter")
    @patch.object(importer, "SigmaClient")
    def test_skip_uses_raw_source_offset_and_already_imported_no_profile(self, source, notion):
        client_obj = source.return_value
        client_obj.attendees.return_value = ([{"id": ID, "first_name": "Test", "business_type": ["Operator"]}], 40)
        notion.return_value.already_imported.return_value = True
        with redirect_stdout(io.StringIO()):
            importer.run()
        client_obj.attendees.assert_called_once_with(2)
        client_obj.profile.assert_not_called()
        notion.return_value.sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import json

from django.test import SimpleTestCase

from rbx.raw_json import member_spans


class MemberSpansTests(SimpleTestCase):
    def _members(self, text):
        return {name: text[start:end] for name, (start, end) in member_spans(text).items()}

    def test_values_are_cut_out_as_written(self):
        text = ' {"a" : 1.50 ,"b":{"y":[1e-5, "x\\u0041"],  "x":null},\n"c":"caf\\u00e9"} '
        self.assertEqual(
            self._members(text),
            {"a": "1.50", "b": '{"y":[1e-5, "x\\u0041"],  "x":null}', "c": '"caf\\u00e9"'},
        )

    def test_a_repeated_name_maps_to_its_last_value_like_json_loads(self):
        text = '{"t": 1, "t": [2]}'
        self.assertEqual(self._members(text), {"t": "[2]"})
        self.assertEqual(json.loads(text)["t"], [2])

    def test_escaped_names_are_decoded(self):
        self.assertEqual(self._members('{"\\u0074x": true}'), {"tx": "true"})

    def test_an_empty_object_has_no_members(self):
        self.assertEqual(member_spans(" { } "), {})

    def test_anything_but_one_json_object_is_refused(self):
        for text in ("", "[]", '"x"', "{", '{"a"}', '{"a":1,}', '{"a":1} x', '{"a":1}{}', '{a:1}', '{"a":1 "b":2}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    member_spans(text)

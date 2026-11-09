from django.test import TestCase


class RetiredRawRoutesTests(TestCase):
    def test_retrieve_smart_contract_is_gone(self):
        response = self.client.get("/api/raw/retrieve-smart-contract/")

        self.assertEqual(response.status_code, 404)

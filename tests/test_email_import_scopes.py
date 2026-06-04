import unittest
from urllib.parse import parse_qs, urlparse

from invest_tax_calc.email_import.oauth import (
    GMAIL_OAUTH,
    build_authorization_url,
)
from invest_tax_calc.email_import.scopes import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPE_POLICY,
    UnsafeScopeError,
)


class ScopePolicyTests(unittest.TestCase):
    def test_gmail_accepts_only_readonly_scope(self):
        GMAIL_SCOPE_POLICY.validate_requested([GMAIL_READONLY_SCOPE])

        with self.assertRaises(UnsafeScopeError):
            GMAIL_SCOPE_POLICY.validate_requested(
                ["https://www.googleapis.com/auth/gmail.modify"]
            )

        with self.assertRaises(UnsafeScopeError):
            GMAIL_SCOPE_POLICY.validate_requested(
                [
                    GMAIL_READONLY_SCOPE,
                    "https://www.googleapis.com/auth/gmail.labels",
                ]
            )

    def test_authorization_url_requests_only_gmail_readonly_scope(self):
        url = build_authorization_url(
            GMAIL_OAUTH,
            client_id="client",
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
            state="state",
            code_challenge="challenge",
        )
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["scope"], [GMAIL_READONLY_SCOPE])


if __name__ == "__main__":
    unittest.main()

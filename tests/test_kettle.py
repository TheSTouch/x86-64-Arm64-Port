import base64
import json
import subprocess
import unittest

from portkit import kettle


def jwt(claims):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'typ': 'JWT', 'alg': 'EdDSA'})}.{part(claims)}.signature"


CLIENT = jwt({"sub": "76561197960287930", "aud": ["client", "web"], "exp": 1900000000})
MOBILE = jwt({"sub": "76561197960287930", "aud": ["mobile"], "exp": 1900000000})


def keychain(token=None):
    """A stand-in for /usr/bin/security: the stored token, or 'not found' (exit code 44)."""
    calls = []

    def security(args):
        calls.append(args)
        if token is None:
            return subprocess.CompletedProcess(args, 44, "", "item not found")
        return subprocess.CompletedProcess(args, 0, token + "\n", "")
    security.calls = calls
    return security


class TokenTest(unittest.TestCase):
    def test_client_token(self):
        self.assertEqual(kettle.token_info(CLIENT), {"steamid": 76561197960287930, "client": True, "expires": 1900000000})

    def test_not_a_token(self):
        for bad in ("", "abc", "a.b.c", "a.!!!.c", jwt({"aud": ["client"]})):
            self.assertIsNone(kettle.token_info(bad), bad)

    def test_keychain_item_kettle_uses(self):
        security = keychain(CLIENT)
        self.assertEqual(kettle.stored_token(environ={}, security=security), CLIENT)
        args = security.calls[0]
        self.assertEqual(args[:2], ["/usr/bin/security", "find-generic-password"])
        self.assertEqual(args[args.index("-s") + 1], "Recomp Steam")
        self.assertEqual(args[args.index("-a") + 1], "steam-client-token")

    def test_environment_wins(self):
        security = keychain(MOBILE)
        self.assertEqual(kettle.stored_token(environ={"RECOMP_STEAM_TOKEN": CLIENT}, security=security), CLIENT)
        self.assertEqual(security.calls, [])

    def test_no_sign_in(self):
        self.assertIsNone(kettle.stored_token(environ={}, security=keychain(None)))

    def test_only_steam_client_tokens_count(self):
        # A Steam Mobile app token cannot log on as a client: Kettle ignores it, so do we.
        self.assertIsNone(kettle.stored_token(environ={}, security=keychain(MOBILE)))


if __name__ == "__main__":
    unittest.main()

"""Update notices show only when signed with the updater key. The fixtures are real output of
`cargo tauri signer generate` / `sign` (a throwaway test key; the app never trusts it)."""
from app.core import checkin

TEXT = b"Please update to 1.2.2."
PUBKEY = ("dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEFFMzQ0OTdGMUNBQUVFNkYKUldSdjdxb2NmMGswcmlxOE"
          "x5TXYzRFJIQ3Nybk85LzJ4NGJ1bTFOa0hXRk5ialplUGJrVTVkNUEK")
SIG = ("dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVSdjdxb2NmMGswcm5Ya0RnQXh0RGVBZDlW"
       "NlNkY3U2R0dWNnJlbGJpaGgxTU1XUFVROVN3MzNKcEMva3VYc2E0eUVoVi8wM0lPQW82UFJEVXNsWWQrNUw2SSszQmFIOVE4PQp0cnVz"
       "dGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkxOTIzODg1CWZpbGU6bm90aWNlLnR4dAo5T2RhVXJRT0hKOUhRVnI0ak1zT1I2aitNeGhX"
       "N0puaWNzbFlwVHcxWnNrWWxVL0sxQ3VYbFNQdHBLNGVqRUp5NmdZM0R1SWp3bGVEeFdZMWdGWmxDZz09Cg==")
# The same text signed by a different key.
OTHER_SIG = ("dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVTRmdNQk43b1NoSDlUU0FDRUdieWs2"
             "blplRXlvYmlDdWJyMThYaWpXcGhyaFVadERudDRucTI5ZnJJaFJGTEUvdkh6RVpqNjVHRUdxd2pvY1ZTU1JIRE53ajgwaHFHSUFn"
             "PQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkxOTI0MzIxCWZpbGU6b3RoZXIudHh0Ck1XTTNHQ2Z4MWE4WCtSbTFTa2xz"
             "Y2h3Rm11RDNHZjBIV24vVXVJQURBQzlWOEwwYTVEdXNQcFhkVVlvT3VVMWNTVjZxdU5vdDg3MWkvbmhyRnp6dkJBPT0K")


def test_signature_from_tauris_signer_verifies():
    assert checkin.signed(TEXT, SIG, PUBKEY)


def test_changed_text_wrong_key_or_garbage_is_refused():
    assert not checkin.signed(b"Please update to 6.6.6.", SIG, PUBKEY)
    assert not checkin.signed(TEXT, OTHER_SIG, PUBKEY)
    assert not checkin.signed(TEXT, "not a signature", PUBKEY)
    assert not checkin.signed(TEXT, SIG, "")


def test_reply_notice_shows_only_when_signed(monkeypatch):
    monkeypatch.setattr(checkin, "UPDATE_PUBKEY", PUBKEY)
    text = TEXT.decode()
    assert checkin._clean_reply({"notice": text, "notice_sig": SIG})["notice"] == text
    assert checkin._clean_reply({"notice": text})["notice"] is None
    assert checkin._clean_reply({"notice": text, "notice_sig": OTHER_SIG})["notice"] is None

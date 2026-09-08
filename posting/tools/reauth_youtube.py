#!/usr/bin/env python3
"""Re-authorize a posting hub YouTube token with the additional
yt-analytics.readonly scope (existing scopes are kept).

Console/manual flow for SSH sessions: prints the consent URL, waits for
the pasted auth code, writes the merged token back atomically.

Usage: python3 reauth_youtube.py <token_path>
"""

import json
import os
import sys

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

USAGE = "usage: python3 reauth_youtube.py <token_path>"


def main(argv):
    if len(argv) != 2:
        print(USAGE)
        return 2
    token_path = argv[1]
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google_auth_oauthlib is not installed "
              "(pip install google-auth-oauthlib)", file=sys.stderr)
        return 1

    with open(token_path) as fh:
        data = json.load(fh)
    if not data.get("client_id") or not data.get("client_secret"):
        print("token file has no client_id/client_secret: %s" % token_path,
              file=sys.stderr)
        return 1

    scopes = list(data.get("scopes") or [])
    if ANALYTICS_SCOPE not in scopes:
        scopes.append(ANALYTICS_SCOPE)

    client_config = {"installed": {
        "client_id": data["client_id"],
        "client_secret": data["client_secret"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": data.get("token_uri",
                              "https://oauth2.googleapis.com/token"),
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob",
                          "http://localhost"]}}
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    flow.redirect_uri = "http://localhost"
    auth_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true",
        prompt="consent")
    print("Открой URL в браузере, войди в Google-аккаунт этого канала "
          "и подтверди доступ:\n\n%s\n\nБраузер после согласия упадёт на "
          "http://localhost — это нормально: скопируй URL из адресной "
          "строки целиком.\n" % auth_url)
    answer = input("Вставь URL редиректа (или сам код): ").strip()
    code = answer
    if "code=" in answer:
        from urllib.parse import parse_qs, urlparse
        code = (parse_qs(urlparse(answer).query).get("code") or [""])[0]
    if not code:
        print("пустой код — токен не изменён", file=sys.stderr)
        return 1
    flow.fetch_token(code=code)
    creds = flow.credentials

    data["token"] = creds.token
    if creds.refresh_token:
        data["refresh_token"] = creds.refresh_token
    data["scopes"] = list(creds.scopes or scopes)
    if creds.expiry is not None:
        exp = creds.expiry
        data["expiry"] = (exp.isoformat() + "Z" if exp.tzinfo is None
                          else exp.isoformat())
    tmp_path = token_path + ".tmp"
    with open(tmp_path, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp_path, token_path)
    print("OK: токен обновлён, scopes: %s" % ", ".join(data["scopes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

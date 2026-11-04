"""`twitter tweet <url> --json`, plus each tweet's `inReplyToId`.

twitter-cli parses X's GraphQL into its own model and drops
`legacy.in_reply_to_status_id_str`, which is the only way to tell whether a
reply in the conversation answers the focal tweet or another reply. This runs
the same CLI command, records that field while twitter-cli parses each tweet,
and adds it to the JSON it prints. Usage: python x_tweet.py <twitter args...>
"""

import contextlib
import io
import json
import sys

from twitter_cli import parser
from twitter_cli.cli import cli

in_reply_to: dict[str, str | None] = {}
_parse_tweet_result = parser.parse_tweet_result


def _recording_parse(result, depth=0):
    tweet = _parse_tweet_result(result, depth)
    if tweet is not None and depth == 0:
        data, _ = parser._unwrap_visibility(result)
        legacy = data.get("legacy") or {}
        in_reply_to[tweet.id] = legacy.get("in_reply_to_status_id_str") or None
    return tweet


# parse_timeline_response looks this name up in the parser module at call time.
parser.parse_tweet_result = _recording_parse

out = io.StringIO()
code = 0
with contextlib.redirect_stdout(out):
    try:
        cli.main(args=sys.argv[1:], prog_name="twitter", standalone_mode=False)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1

text = out.getvalue()
try:
    payload = json.loads(text)
except ValueError:
    sys.stdout.write(text)
    sys.exit(code)
if isinstance(payload, dict) and isinstance(payload.get("data"), list):
    for tweet in payload["data"]:
        if isinstance(tweet, dict):
            tweet["inReplyToId"] = in_reply_to.get(str(tweet.get("id")))
json.dump(payload, sys.stdout, ensure_ascii=False)
sys.exit(code)

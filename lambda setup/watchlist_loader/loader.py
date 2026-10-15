import re
import urllib.parse
import load

KEY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv$")

def handler(event, context):
    for rec in event["Records"]:
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        m = KEY_RE.search(key)
        if not key.startswith(load.PREFIX) or not m:
            print(f"skipping {key}")
            continue
        load.load_day(m.group(1))
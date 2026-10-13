"""小黑盒公开网页请求参数；与官网网页的签名规则保持一致。"""

import hashlib
import itertools
import time
import uuid


def heybox_params(path):
    stamp = int(time.time())
    nonce = uuid.uuid4().hex.upper()
    alphabet = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

    def mapped(value, chars):
        return "".join(chars[ord(c) % len(chars)] for c in value)

    path = "/" + "/".join(filter(None, path.split("/"))) + "/"
    parts = [
        mapped(str(stamp + 1), alphabet[:-2]),
        mapped(path, alphabet),
        mapped(nonce, alphabet),
    ]
    mixed = "".join("".join(row) for row in itertools.zip_longest(*parts, fillvalue=""))[:20]
    digest = hashlib.md5(mixed.encode()).hexdigest()

    def double(value):
        return ((value << 1) ^ 27) & 255 if value & 128 else value << 1

    def triple(value):
        return double(value) ^ value

    def six(value):
        return triple(double(value))

    def eleven(value):
        return six(triple(double(value)))

    def fourteen(value):
        return eleven(value) ^ six(value) ^ triple(value)

    a, b, c, d, e, f = map(ord, digest[-6:])
    total = sum(
        (
            fourteen(a) ^ eleven(b) ^ six(c) ^ triple(d),
            triple(a) ^ fourteen(b) ^ eleven(c) ^ six(d),
            six(a) ^ triple(b) ^ fourteen(c) ^ eleven(d),
            eleven(a) ^ six(b) ^ triple(c) ^ fourteen(d),
            e,
            f,
        )
    )
    return {
        "hkey": mapped(digest[:5], alphabet[:-4]) + f"{total % 100:02}",
        "_time": stamp,
        "nonce": nonce,
        "version": "999.0.4",
        "os_type": "web",
        "app": "heybox",
        "client_type": "web",
        "x_client_type": "web",
        "x_os_type": "Windows",
        "x_app": "heybox_website",
        "web_version": "3.0",
        "heybox_id": "-1",
        "offset": 0,
        "limit": 1,
    }

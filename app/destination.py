"""LLMの送り先が、手元のネットワークの中か外か。

秘密の情報を質問に入れてよいか判断できるよう、画面に出す。
LLM_BASE_URL のホストを名前解決し、どのアドレスもループバック・プライベート・リンクローカルなら「ローカル」、
1つでもインターネット上のアドレスなら「外部」とする。名前解決できなければ「不明」。
"""

import ipaddress
import socket
import urllib.parse
from functools import lru_cache


@lru_cache(maxsize=4)
def classify(base_url: str) -> dict:
    """{"kind": "local" | "external" | "unknown", "host", "addresses"}"""
    host = urllib.parse.urlsplit(base_url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return {"kind": "unknown", "host": host, "addresses": []}
    addresses = sorted({info[4][0] for info in infos})
    ips = [ipaddress.ip_address(a.split("%")[0]) for a in addresses]
    local = all(ip.is_loopback or ip.is_private or ip.is_link_local for ip in ips)
    return {"kind": "local" if ips and local else "external", "host": host, "addresses": addresses}

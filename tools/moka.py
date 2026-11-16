"""Moka 招聘云通用抓取器。

中兴、大疆、宁德时代（还有一堆别的公司）的校招站都是 Moka 建的，
接口完全一样，只是 orgId / siteId / 门户地址不同。所以写一份，三家共用。

两个坑：
1. 接口只认 POST，参数全在 JSON body 里（GET 会 405）。
2. 返回体是 AES-128-CBC 加密过的 base64：
      {"data": "<base64>", "necromancer": "<16 位>"}
   key = necromancer 的 UTF-8 字节；iv 在门户页 HTML 的
   <input id="init-data" value="{...aesIv...}"> 里。
   本机没有 pycryptodome，所以下面手写了一个 AES 解密。
"""

import base64
import gzip
import html
import http.cookiejar
import json
import ssl
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    urllib.request.HTTPSHandler(context=CTX),
)


# ------------------------------------------------------------------ 网络
def http(url, body=None, referer=None, tries=3):
    """GET（body=None）或 POST JSON，自动解 gzip，失败重试。"""
    last = None
    for k in range(tries):
        try:
            headers = {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate",
            }
            if referer:
                headers["Referer"] = referer
            if body is not None:
                headers["Content-Type"] = "application/json;charset=UTF-8"
                data = json.dumps(body).encode("utf-8")
            else:
                data = None
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
            with _OPENER.open(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:            # noqa: BLE001 —— 网络错误类型太多，统一重试
            last = e
            time.sleep(1.5 * (k + 1))
    raise last


# ------------------------------------------------------------------ AES 解密
def _build_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= ((x << 1) ^ (0x1B if x & 0x80 else 0)) & 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    def mul(a, b):
        return 0 if not a or not b else exp[log[a] + log[b]]

    sbox = [0] * 256
    for a in range(256):
        b = exp[255 - log[a]] if a else 0
        s = b
        for _ in range(4):
            b = ((b << 1) | (b >> 7)) & 0xFF
            s ^= b
        sbox[a] = s ^ 0x63

    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return exp, log, mul, sbox, inv


_EXP, _LOG, _MUL, _SBOX, _INV = _build_tables()


def aes_cbc_dec(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密 + 去 PKCS7 填充。纯标准库实现。"""
    S, IS, mul = _SBOX, _INV, _MUL

    # 扩展密钥（128 位 → 11 组 round key）
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [S[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = ((rcon << 1) ^ (0x1B if rcon & 0x80 else 0)) & 0xFF
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    rk = [sum(w[4 * r:4 * r + 4], []) for r in range(11)]

    out = b""
    prev = list(iv)
    for i in range(0, len(data), 16):
        block = list(data[i:i + 16])
        s = [block[j] ^ rk[10][j] for j in range(16)]
        for rnd in range(9, -1, -1):
            # InvShiftRows
            s = [IS[s[((j // 4) - (j % 4)) % 4 * 4 + j % 4]] for j in range(16)]
            # AddRoundKey
            s = [s[j] ^ rk[rnd][j] for j in range(16)]
            if rnd:
                # InvMixColumns
                ns = [0] * 16
                for c in range(4):
                    a0, a1, a2, a3 = s[c * 4:c * 4 + 4]
                    ns[c * 4:c * 4 + 4] = [
                        mul(a0, 14) ^ mul(a1, 11) ^ mul(a2, 13) ^ mul(a3, 9),
                        mul(a0, 9) ^ mul(a1, 14) ^ mul(a2, 11) ^ mul(a3, 13),
                        mul(a0, 13) ^ mul(a1, 9) ^ mul(a2, 14) ^ mul(a3, 11),
                        mul(a0, 11) ^ mul(a1, 13) ^ mul(a2, 9) ^ mul(a3, 14),
                    ]
                s = ns
        out += bytes(s[j] ^ prev[j] for j in range(16))
        prev = block
    pad = out[-1]
    return out[:-pad] if 1 <= pad <= 16 else out


# ------------------------------------------------------------------ Moka
def get_aes_iv(portal_url: str) -> bytes:
    """门户页里 <input id="init-data" value="..."> 的 aesIv。"""
    page = http(portal_url, referer=portal_url).decode("utf-8", "replace")
    p = page.index('id="init-data"')
    q = page.index('value="', p) + 7
    raw = html.unescape(page[q:page.index('"', q)])
    return json.loads(raw)["aesIv"].encode("utf-8")


def fetch_jobs(api_url: str, org_id: str, site_id: str, iv: bytes,
               offset: int = 0, limit: int = 50, keyword: str = "") -> dict:
    """取一页岗位，返回解密后的 data（含 jobs 和 jobStats）。"""
    body = {
        "orgId": org_id, "siteId": site_id, "locale": "zh-CN",
        "needStat": True, "limit": limit, "offset": offset, "keyword": keyword,
    }
    resp = json.loads(http(api_url, body=body, referer=api_url).decode("utf-8", "ignore"))
    plain = aes_cbc_dec(base64.b64decode(resp["data"]), resp["necromancer"].encode("utf-8"), iv)
    return json.loads(plain.decode("utf-8", "ignore"))["data"]


def all_jobs(portal_url: str, org_id: str, site_id: str, max_items: int = 30) -> list:
    """翻页取全部岗位（最多 max_items 条）。"""
    api = portal_url.rstrip("/").split("/campus-recruitment")[0] + "/api/outer/ats-apply/website/jobs/v2"
    iv = get_aes_iv(portal_url)
    first = fetch_jobs(api, org_id, site_id, iv)
    total = (first.get("jobStats") or {}).get("total") or 0
    rows = list(first.get("jobs") or [])
    off = 50
    while len(rows) < min(total, max_items) and off < total:
        rows += fetch_jobs(api, org_id, site_id, iv, offset=off).get("jobs") or []
        off += 50
    print("    Moka 共 %s 条，取 %d 条" % (total, len(rows[:max_items])))
    return rows[:max_items]

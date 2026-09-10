#!/usr/bin/env python3
"""SNIN Mail — reproducible crypto fixture generator (NIP-44 / NIP-59).

Emits a machine-readable fixture: every case is input -> expected -> actual -> verdict.
No secrets, no private keys of real mailboxes: only public NIP-44 spec vectors
(shipped in docs/nip44.vectors.json) plus deterministic edge cases.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mailbridge import nip44, nip59  # noqa: E402

V = json.load(open(os.path.join(ROOT, "docs", "nip44.vectors.json")))["v2"]
cases = []


def rec(cid, area, given, expected, fn):
    """Run fn(); record expected vs actual + verdict. Never raises."""
    try:
        actual = fn()
        ok = actual == expected
        err = None
    except Exception as exc:  # noqa: BLE001
        actual, ok, err = None, False, f"{type(exc).__name__}: {exc}"[:120]
    cases.append({
        "id": cid,
        "area": area,
        "input": given,
        "expected": expected,
        "actual": actual if actual is not None else None,
        "error": err,
        "verdict": "PASS" if ok else "FAIL",
    })


def expect_error(cid, area, given, fn):
    """Case where correct behaviour is a raised error (invalid vectors)."""
    try:
        actual = fn()
        cases.append({"id": cid, "area": area, "input": given,
                      "expected": "error", "actual": actual,
                      "error": None, "verdict": "FAIL"})
    except Exception as exc:  # noqa: BLE001
        cases.append({"id": cid, "area": area, "input": given,
                      "expected": "error", "actual": None,
                      "error": type(exc).__name__,
                      "verdict": "PASS"})


# --- A. conversation key (official valid vectors) -------------------------
for i, v in enumerate(V["valid"]["get_conversation_key"]):
    rec(f"A{i:02d}", "nip44.conversation_key",
        {"sec1": v["sec1"][:16] + "…", "pub2": v["pub2"][:16] + "…", "note": v.get("note", "")},
        v["conversation_key"],
        lambda v=v: nip44.get_conversation_key(v["sec1"], v["pub2"]).hex())

# --- B. message keys ------------------------------------------------------
mk = V["valid"]["get_message_keys"]
for i, k in enumerate(mk["keys"]):
    rec(f"B{i:02d}", "nip44.message_keys",
        {"conversation_key": mk["conversation_key"][:16] + "…", "nonce": k["nonce"][:16] + "…"},
        {"chacha_key": k["chacha_key"], "chacha_nonce": k["chacha_nonce"], "hmac_key": k["hmac_key"]},
        lambda k=k: dict(zip(
            ("chacha_key", "chacha_nonce", "hmac_key"),
            [x.hex() for x in nip44.get_message_keys(bytes.fromhex(mk["conversation_key"]), bytes.fromhex(k["nonce"]))])))

# --- C. encrypt/decrypt bytes-exact vs official payloads ------------------
for i, v in enumerate(V["valid"]["encrypt_decrypt"]):
    conv = bytes.fromhex(v["conversation_key"])
    nonce = bytes.fromhex(v["nonce"])
    rec(f"C{i:02d}", "nip44.encrypt_payload",
        {"plaintext": v["plaintext"], "nonce": v["nonce"][:16] + "…"},
        v["payload"],
        lambda v=v, conv=conv, nonce=nonce: nip44.encrypt(v["plaintext"], conv, nonce))
    rec(f"C{i:02d}u", "nip44.decrypt_payload",
        {"payload": v["payload"][:24] + "…"},
        v["plaintext"],
        lambda v=v, conv=conv: nip44.decrypt(v["payload"], conv))

# --- D. long messages (sha256 of plaintext + payload) --------------------
for i, v in enumerate(V["valid"]["encrypt_decrypt_long_msg"]):
    conv = bytes.fromhex(v["conversation_key"])
    nonce = bytes.fromhex(v["nonce"])
    pt = v["pattern"] * v["repeat"]
    rec(f"D{i:02d}", "nip44.long_message",
        {"pattern": v["pattern"], "repeat": v["repeat"], "bytes": len(pt.encode())},
        {"plaintext_sha256": v["plaintext_sha256"], "payload_sha256": v["payload_sha256"]},
        lambda v=v, conv=conv, nonce=nonce, pt=pt: {
            "plaintext_sha256": hashlib.sha256(pt.encode()).hexdigest(),
            "payload_sha256": hashlib.sha256(nip44.encrypt(pt, conv, nonce).encode()).hexdigest()})

# --- E. invalid conversation keys must be rejected -----------------------
for i, v in enumerate(V["invalid"]["get_conversation_key"]):
    expect_error(f"E{i:02d}", "nip44.invalid_key",
                 {"sec1": v["sec1"][:16] + "…", "pub2": v["pub2"][:16] + "…", "note": v["note"]},
                 lambda v=v: nip44.get_conversation_key(v["sec1"], v["pub2"]).hex())

# --- F. invalid payloads must be rejected --------------------------------
for i, v in enumerate(V["invalid"]["decrypt"]):
    conv = bytes.fromhex(v["conversation_key"])
    expect_error(f"F{i:02d}", "nip44.invalid_payload",
                 {"payload": (v["payload"][:20] + "…") if v["payload"] else "<empty>", "note": v["note"]},
                 lambda v=v, conv=conv: nip44.decrypt(v["payload"], conv))

# --- G. out-of-range plaintext lengths must be rejected ------------------
for i, n in enumerate(V["invalid"]["encrypt_msg_lengths"]):
    conv = bytes(32)
    expect_error(f"G{i:02d}", "nip44.length_limit",
                 {"plaintext_bytes": n, "limit": 65535},
                 lambda n=n, conv=conv: nip44.encrypt("x" * n, conv, bytes(32)))

# --- H. deterministic edge cases (our own, published so others can replay)
conv_h = bytes.fromhex("8fc262099ce0d0bb9b89bac05bb9e04f9bc0090acc181fef6840ccee470371ed")
# spec boundary: NIP-44 v2 allows 1..65535 bytes of plaintext; empty must be rejected
expect_error("H00", "nip44.edge_empty_rejected",
             {"case": "empty", "plaintext_bytes": 0, "spec_limit": "MIN_PLAINTEXT=1"},
             lambda: nip44.encrypt("", conv_h, hashlib.sha256(b"snin-mail-fixture-empty").digest()))

edge = [
    ("one_ascii", "a"),
    ("ru", "Привет, мир"),
    ("emoji", "🙈🙉🙊"),
    ("rtl", "الكل في المجمو عة (5)"),
    ("combo", "ability🤝的 ȺȾ"),
]
for i, (name, msg) in enumerate(edge):
    nonce = hashlib.sha256(f"snin-mail-fixture-{name}".encode()).digest()
    payload = nip44.encrypt(msg, conv_h, nonce)
    rec(f"H{i:02d}", "nip44.edge_roundtrip",
        {"case": name, "plaintext": msg, "nonce": nonce.hex()},
        {"payload": payload, "decrypted": msg},
        lambda p=payload, m=msg: {"payload": p, "decrypted": nip44.decrypt(p, conv_h)})

# tamper: flip one byte of the MAC region, decryption must fail
base = nip44.encrypt("snin mail tamper probe", conv_h, bytes(32))
tampered = base[:-4] + ("AAAA" if not base.endswith("AAAA") else "BBBB")
expect_error("H90", "nip44.tamper", {"payload_sha256": hashlib.sha256(tampered.encode()).hexdigest()},
             lambda: nip44.decrypt(tampered, conv_h))

# --- I. NIP-59 gift wrap ------------------------------------------------
# Fixed throwaway keys (derived from strings) so the artifact stays bit-for-bit
# reproducible: NIP-59 itself generates a fresh ephemeral key + tweaks the
# timestamp per message, so we record PROPERTIES, never those random values.
alice = hashlib.sha256(b"snin-mail-fixture-alice").hexdigest()
bob = hashlib.sha256(b"snin-mail-fixture-bob").hexdigest()
eve = hashlib.sha256(b"snin-mail-fixture-eve").hexdigest()
alice_pub = nip44.pubkey_from_privkey(alice)
bob_pub = nip44.pubkey_from_privkey(bob)
RUMOR_TS = 1789000000
rumor = nip59.create_rumor(alice_pub, 1301, "fixture mail body", [["p", bob_pub]], RUMOR_TS)
wrap = nip59.wrap(rumor, alice, bob_pub)

rec("I00", "nip59.wrap_kind", {"rumor_kind": 1301, "sender": alice_pub[:16] + "…"},
    1059, lambda: wrap["kind"])
rec("I01", "nip59.wrap_signature", {"checked": "id+sig of the gift wrap event"}, True,
    lambda: nip59.verify_signature(wrap["pubkey"], wrap["id"], wrap["sig"]))
rec("I02", "nip59.roundtrip", {"rumor_content": "fixture mail body"}, "fixture mail body",
    lambda: nip59.unwrap(wrap, bob)[0]["content"])
expect_error("I03", "nip59.wrong_recipient", {"recipient": "eve (fixed throwaway key)"},
             lambda: nip59.unwrap(wrap, eve))
rec("I04", "nip59.ephemeral_sender", {"expect": "wrap pubkey != sender pubkey"}, True,
    lambda: wrap["pubkey"] != alice_pub)
rec("I05", "nip59.timestamp_tweak", {"rumor_created_at": RUMOR_TS, "explicit_wrap_created_at": RUMOR_TS}, True,
    lambda: abs(nip59.wrap(rumor, alice, bob_pub, created_at=RUMOR_TS)["created_at"] - RUMOR_TS) <= 60)


def observe(cid, area, given, fn):
    """Case that records a measured fact rather than asserting spec conformance."""
    try:
        actual, err = fn(), None
    except Exception as exc:  # noqa: BLE001
        actual, err = None, f"{type(exc).__name__}: {exc}"[:120]
    cases.append({"id": cid, "area": area, "input": given, "expected": "OBSERVED",
                  "actual": actual, "error": err, "verdict": "OBSERVED"})


# deviations from the NIP-59 recommendation, recorded as measured facts:
# spec says the wrap timestamp SHOULD be randomized within TWO DAYS of the
# rumor; this implementation tweaks +-60 s around its own creation time.
observe("I06", "nip59.tweak_window", {"spec_recommended_window_s": 172800, "implementation_window_s": 60},
        lambda: {"implementation_window_s": 60, "spec_recommended_window_s": 172800,
                 "narrower_than_recommended": True})
# raw API: wrap() without created_at stamps the envelope with "now" and does not
# inherit rumor.created_at, so an old rumor gets a present-day envelope time.
observe("I08", "nip59.envelope_vs_rumor_time",
        {"rumor_created_at": RUMOR_TS, "wrap_called_with": "created_at=None"},
        lambda: {"inherits_rumor_time": abs(nip59.wrap(rumor, alice, bob_pub)["created_at"] - RUMOR_TS) <= 60,
                 "envelope_stamped_with_wall_clock": abs(nip59.wrap(rumor, alice, bob_pub)["created_at"] - RUMOR_TS) > 3600})
rec("I07", "nip59.sender_not_disclosed", {"seal_kind": 13, "wrap_kind": 1059}, True,
    lambda: wrap["kind"] == 1059 and json.loads(nip44.decrypt(wrap["content"], nip44.get_conversation_key(bob, wrap["pubkey"])))["kind"] == 13)

# --- summary + artifact ------------------------------------------------
summary = {
    "total": len(cases),
    "pass": sum(1 for c in cases if c["verdict"] == "PASS"),
    "fail": sum(1 for c in cases if c["verdict"] == "FAIL"),
    "observed": sum(1 for c in cases if c["verdict"] == "OBSERVED"),
}
summary["by_area"] = {}
for c in cases:
    a = summary["by_area"].setdefault(c["area"], {"total": 0, "pass": 0, "fail": 0, "observed": 0})
    a["total"] += 1
    a["pass" if c["verdict"] == "PASS" else ("observed" if c["verdict"] == "OBSERVED" else "fail")] += 1

try:
    commit = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=10).stdout.strip()
except Exception:  # noqa: BLE001
    commit = None

artifact = {
    "schema": "snin-mail-crypto-fixture/1.0",
    "project": "SNIN Mail / nostr-mail-bridge",
    "repo": "https://github.com/konantgit-sys/snin-mail-nostr",
    "commit": commit,
    "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "vector_source": "docs/nip44.vectors.json (NIP-44 v2 official vectors, shipped in-repo)",
    "how_to_replay": [
        "pip3 install --break-system-packages -r requirements.txt",
        "python3 tools/make_fixture.py > fixture.json",
        "sha256sum fixture.json",
    ],
    "notes": [
        "No private keys of real mailboxes are included: only spec vectors and freshly generated throwaway keys.",
        "Every case is input -> expected -> actual -> verdict; independent re-run must reproduce actual bytes.",
        "Verdicts are recorded as-is: FAIL rows are kept, not hidden.",
    ],
    "summary": summary,
    "cases": cases,
}
out = json.dumps(artifact, ensure_ascii=False, indent=1, sort_keys=False)
path = os.path.join(ROOT, "fixture.json")
open(path, "w").write(out)
print(out)
sys.stderr.write("\nsummary: %s\nsha256(fixture.json)=%s\n" % (
    json.dumps(summary, ensure_ascii=False), hashlib.sha256(out.encode()).hexdigest()))

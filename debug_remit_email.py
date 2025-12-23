from dotenv import load_dotenv
load_dotenv()

import os, imaplib, email
from email.header import decode_header

def dh(v):
    if not v: return ""
    parts = decode_header(v)
    out=[]
    for b,enc in parts:
        if isinstance(b, bytes):
            out.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(b)
    return "".join(out)

u=os.getenv("DF_REMITTANCE_IMAP_USER")
p=os.getenv("DF_REMITTANCE_IMAP_PASS")
lab=os.getenv("DF_REMITTANCE_GMAIL_LABEL")
mb=os.getenv("DF_REMITTANCE_IMAP_MAILBOX") or "INBOX"

print("user?", bool(u), "pass?", bool(p), "label=", lab, "mailbox=", mb)

im=imaplib.IMAP4_SSL("imap.gmail.com")
im.login(u,p)
im.select(mb)

typ,data=im.search(None, "X-GM-LABELS", f"\"{lab}\"")
ids=(data[0].split() if data and data[0] else [])
print("found", len(ids))
if not ids:
    im.logout()
    raise SystemExit()

# Pick the newest labeled email
num = ids[-1]
typ,msg_data = im.fetch(num, "(RFC822)")
raw = None
for chunk in msg_data:
    if isinstance(chunk, tuple) and chunk[1]:
        raw = chunk[1]
        break

msg = email.message_from_bytes(raw)
print("SUBJECT:", dh(msg.get("Subject")))
print("FROM:", dh(msg.get("From")))

# Print first 3000 chars of body (prefer text/html, else text/plain)
body = None
ctype = None
if msg.is_multipart():
    html = None
    plain = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        pt = part.get_payload(decode=True) or b""
        cs = part.get_content_charset() or "utf-8"
        txt = pt.decode(cs, errors="replace")
        if part.get_content_type() == "text/html" and html is None:
            html = txt
        if part.get_content_type() == "text/plain" and plain is None:
            plain = txt
    body = html if html is not None else plain
    ctype = "text/html" if html is not None else "text/plain"
else:
    pt = msg.get_payload(decode=True) or b""
    cs = msg.get_content_charset() or "utf-8"
    body = pt.decode(cs, errors="replace")
    ctype = msg.get_content_type()

print("BODY_TYPE:", ctype)
print("BODY_HEAD:\n", (body or "")[:3000])

im.logout()

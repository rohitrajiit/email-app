import os, imaplib, smtplib
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr, formatdate
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY","dev")

EMAIL = os.environ.get("EMAIL_ADDRESS")
PASS = os.environ.get("EMAIL_PASSWORD")
IMAP_SERVER = os.environ.get("IMAP_SERVER","imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT","993"))
SMTP_SERVER = os.environ.get("SMTP_SERVER","smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT","465"))
MAILBOX = os.environ.get("MAILBOX","INBOX")

def _dec(s):
    if not s: return ""
    try: return str(make_header(decode_header(s)))
    except: return s

def _addr(s):
    name, addr = parseaddr(s or "")
    return f"{name} <{addr}>" if name else addr

def imap_conn():
    m = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    m.login(EMAIL, PASS)
    return m

@app.route("/")
def inbox():
    q = request.args.get("q","").strip()
    limit = int(request.args.get("limit","25"))
    emails = []
    try:
        m = imap_conn(); m.select(MAILBOX)
        crit = 'ALL'
        if q:
            crit = f'(OR (HEADER Subject "{q}") (HEADER From "{q}"))'
        typ, data = m.uid("SEARCH", None, crit)
        ids = (data[0].split() if typ=="OK" else [])
        ids = ids[-limit:][::-1]
        for i in ids:
            uid = i.decode()
            typ, d = m.uid("FETCH", uid, "(RFC822.HEADER)")
            if typ!="OK" or not d or d[0] is None: continue
            raw = d[0][1]
            from email import message_from_bytes as mfb
            msg = mfb(raw)
            emails.append({
                "uid": uid,
                "from": _addr(msg.get("From")),
                "subject": _dec(msg.get("Subject")) or "(no subject)",
                "date": msg.get("Date"),
            })
        m.logout()
    except Exception as e:
        flash(f"IMAP error: {e}")
    return render_template("inbox.html", emails=emails)

def fetch_full(uid):
    m = imap_conn(); m.select(MAILBOX)
    typ, data = m.uid("FETCH", uid, "(RFC822)")
    m.logout()
    if typ!="OK" or not data or data[0] is None: return None
    raw = data[0][1]
    from email import message_from_bytes as mfb
    msg = mfb(raw)
    text = html = None
    atts = []
    idx = 0
    if msg.is_multipart():
        for part in msg.walk():
            cdisp = (part.get("Content-Disposition") or "").lower()
            ctype = (part.get_content_type() or "").lower()
            if ctype=="text/plain" and "attachment" not in cdisp and text is None:
                text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype=="text/html" and "attachment" not in cdisp and html is None:
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            elif "attachment" in cdisp or part.get_filename():
                payload = part.get_payload(decode=True) or b""
                fname = part.get_filename() or f"attachment-{idx}.bin"
                atts.append({"part_index": idx, "filename": _dec(fname), "size": len(payload)})
            idx += 1
    else:
        ctype = (msg.get_content_type() or "").lower()
        payload = msg.get_payload(decode=True) or b""
        if ctype=="text/plain":
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        elif ctype=="text/html":
            html = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return {
        "uid": uid, "from": _addr(msg.get("From")), "to": _addr(msg.get("To")),
        "subject": _dec(msg.get("Subject")) or "(no subject)", "date": msg.get("Date"),
        "text": text, "html": html, "attachments": atts
    }

@app.route("/message/<uid>")
def view_message(uid):
    m = fetch_full(uid)
    if not m:
        flash("Message not found"); return redirect(url_for("inbox"))
    return render_template("view.html", message=m)

@app.route("/attachment/<uid>/<int:idx>")
def download_attachment(uid, idx):
    import email as _e
    m = imap_conn(); m.select(MAILBOX)
    typ, data = m.uid("FETCH", uid, "(RFC822)"); m.logout()
    if typ!="OK" or not data or data[0] is None: return ("Not found",404)
    msg = _e.message_from_bytes(data[0][1])
    i = 0
    for part in msg.walk():
        if i==idx:
            fname = part.get_filename() or f"attachment-{idx}.bin"
            payload = part.get_payload(decode=True) or b""
            return send_file(BytesIO(payload), as_attachment=True, download_name=fname)
        i += 1
    return ("Not found",404)

@app.route("/compose")
def compose():
    return render_template("compose.html")

@app.route("/send", methods=["POST"])
def send_mail():
    to = request.form.get("to","").strip()
    subject = request.form.get("subject","").strip()
    body = request.form.get("body","").strip()
    if not to or not subject or not body:
        flash("To, Subject, Body are required"); return redirect(url_for("compose"))
    msg = MIMEMultipart()
    msg["From"] = EMAIL; msg["To"] = to; msg["Date"] = formatdate(localtime=True); msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    for f in request.files.getlist("attachments"):
        if not f or not f.filename: continue
        part = MIMEBase("application","octet-stream"); payload = f.read()
        part.set_payload(payload); encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{f.filename}"')
        msg.attach(part)
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as s:
            s.login(EMAIL, PASS); s.sendmail(EMAIL, [to], msg.as_string())
        flash("Email sent"); return redirect(url_for("inbox"))
    except Exception as e:
        flash(f"SMTP error: {e}"); return redirect(url_for("compose"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5000")), debug=True)

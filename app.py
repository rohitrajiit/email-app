import os
import re
import imaplib
import smtplib
import mimetypes
from email import policy
from email.utils import formatdate, make_msgid, getaddresses
from email.header import decode_header
from email.parser import BytesParser
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev")

# IMAP/SMTP config
IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
IMAP_USER = os.getenv("IMAP_USERNAME")
IMAP_PASS = os.getenv("IMAP_PASSWORD")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
SMTP_USER = os.getenv("SMTP_USERNAME")
SMTP_PASS = os.getenv("SMTP_PASSWORD")
SMTP_ENC  = os.getenv("SMTP_ENCRYPTION", "ssl").lower()  # 'ssl' or 'starttls'
FROM_NAME = os.getenv("FROM_NAME", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

# ---------- Helpers ----------

def _decode_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                decoded.append(text.decode(enc or "utf-8", errors="replace"))
            except Exception:
                decoded.append(text.decode("utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _html_to_text(html: str) -> str:
    # Lightweight HTML → text for previews
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n+", "\n", text).strip()


def fetch_inbox(limit=25):
    """Return a list of dicts: [{uid, subject, from, date, snippet}]"""
    msgs = []
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as M:
        M.login(IMAP_USER, IMAP_PASS)
        typ, _ = M.select(IMAP_FOLDER)
        if typ != 'OK':
            raise RuntimeError("Cannot select folder")

        typ, data = M.uid('search', None, 'ALL')
        if typ != 'OK' or not data or not data[0]:
            return []

        uids = data[0].split()
        for uid in reversed(uids[-limit:]):  # newest first
            typ, msg_data = M.uid('fetch', uid, '(BODY.PEEK[])')
            if typ != 'OK' or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = BytesParser(policy=policy.default).parsebytes(raw)

            subject = _decode_header(msg['subject'])
            from_ = _decode_header(msg.get('from', ''))
            date = msg.get('date', '')

            # Extract a preview
            snippet = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    disp = (part.get('Content-Disposition') or '').lower()
                    if ctype == 'text/plain' and 'attachment' not in disp:
                        try:
                            snippet = part.get_content().strip()
                            break
                        except Exception:
                            pass
                if not snippet:
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        disp = (part.get('Content-Disposition') or '').lower()
                        if ctype == 'text/html' and 'attachment' not in disp:
                            try:
                                snippet = _html_to_text(part.get_content())
                                break
                            except Exception:
                                pass
            else:
                ctype = msg.get_content_type()
                if ctype == 'text/plain':
                    snippet = msg.get_content().strip()
                elif ctype == 'text/html':
                    snippet = _html_to_text(msg.get_content())

            snippet = (snippet or "").replace("\r", "\n").splitlines()
            snippet = " ".join(line.strip() for line in snippet if line.strip())
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"

            msgs.append({
                'uid': uid.decode('ascii'),
                'subject': subject or '(no subject)',
                'from': from_,
                'date': date,
                'snippet': snippet,
            })
    return msgs


def fetch_message(uid: str):
    """Return a dict with headers and best-effort text & html bodies."""
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as M:
        M.login(IMAP_USER, IMAP_PASS)
        typ, _ = M.select(IMAP_FOLDER)
        if typ != 'OK':
            raise RuntimeError("Cannot select folder")
        typ, msg_data = M.uid('fetch', uid, '(BODY.PEEK[])')
        if typ != 'OK' or not msg_data or not msg_data[0]:
            return None
        raw = msg_data[0][1]
        msg = BytesParser(policy=policy.default).parsebytes(raw)

        subject = _decode_header(msg['subject'])
        from_ = _decode_header(msg.get('from', ''))
        to_ = _decode_header(msg.get('to', ''))
        cc_ = _decode_header(msg.get('cc', ''))
        date = msg.get('date', '')

        text_body = None
        html_body = None

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = (part.get('Content-Disposition') or '').lower()
                if ctype == 'text/plain' and 'attachment' not in disp and text_body is None:
                    try:
                        text_body = part.get_content().strip()
                    except Exception:
                        pass
                elif ctype == 'text/html' and 'attachment' not in disp and html_body is None:
                    try:
                        html_body = part.get_content()
                    except Exception:
                        pass
        else:
            ctype = msg.get_content_type()
            if ctype == 'text/plain':
                text_body = msg.get_content().strip()
            elif ctype == 'text/html':
                html_body = msg.get_content()

        if text_body is None and html_body is not None:
            text_body = _html_to_text(html_body)
        if html_body is None and text_body is not None:
            html_body = '<pre class="whitespace-pre-wrap">' + re.escape(text_body) + '</pre>'

        return {
            'uid': uid,
            'subject': subject or '(no subject)',
            'from': from_,
            'to': to_,
            'cc': cc_,
            'date': date,
            'text': text_body or '',
            'html': html_body or '',
        }


def send_email(to_field: str, subject: str, body: str, attachment):
    msg = EmailMessage()
    msg['Subject'] = subject or ''
    msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>" if FROM_NAME else FROM_EMAIL
    msg['To'] = to_field
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid()

    # Plain + HTML (very simple). Clients will display the best available.
    msg.set_content(body)
    msg.add_alternative(f"""
    <html>
      <body>
        <div>{body.replace('\n','<br>')}</div>
      </body>
    </html>
    """, subtype='html')

    # Optional single attachment
    if attachment and attachment.filename:
        data = attachment.read()
        ctype, encoding = mimetypes.guess_type(attachment.filename)
        if ctype is None:
            maintype, subtype = 'application', 'octet-stream'
        else:
            maintype, subtype = ctype.split('/', 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment.filename)

    if SMTP_ENC == 'ssl':
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


# ---------- Routes ----------

@app.route('/')
def inbox():
    try:
        messages = fetch_inbox(limit=25)
    except Exception as e:
        flash(f"Error loading inbox: {e}", "error")
        messages = []
    return render_template('index.html', messages=messages)


@app.route('/email/<uid>')
def view_email(uid):
    data = fetch_message(uid)
    if not data:
        flash("Message not found", "error")
        return redirect(url_for('inbox'))
    return render_template('view.html', m=data)


@app.route('/compose', methods=['GET', 'POST'])
def compose():
    if request.method == 'POST':
        to_field = (request.form.get('to') or '').strip()
        subject  = (request.form.get('subject') or '').strip()
        body     = (request.form.get('body') or '').strip()
        file     = request.files.get('attachment')

        # Basic validation for recipient
        if not to_field:
            flash('Please enter a recipient email.', 'error')
            return redirect(url_for('compose'))

        try:
            send_email(to_field, subject, body, file)
            flash('Em

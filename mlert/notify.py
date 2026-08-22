"""Email digest composition and Gmail SMTP delivery."""

import html
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .mercari import item_url

_CSS_CARD = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "margin:0 0 18px 0;padding:12px;border:1px solid #e6e6e6;border-radius:10px;"
)


def _yen(v):
    return f"¥{int(v):,}" if isinstance(v, (int, float)) else "?"


def _card_html(hit, median=None):
    url = item_url(hit["id"])
    title = html.escape(hit.get("name") or "(no title)")
    price = _yen(hit.get("price"))
    img = hit.get("thumbnail")

    flags = []
    if hit.get("deal") and median:
        flags.append(
            f"<span style='background:#e8f5e9;color:#1b5e20;padding:2px 7px;"
            f"border-radius:10px;font-size:12px;'>below median ({_yen(median)})</span>"
        )
    if hit.get("relist"):
        flags.append(
            "<span style='background:#fff4e5;color:#8a5300;padding:2px 7px;"
            "border-radius:10px;font-size:12px;'>relist</span>"
        )
    flag_html = " ".join(flags)

    hits = hit.get("hits") or []
    why = html.escape(", ".join(hits[:8])) if hits else "-"

    img_html = (
        f"<td width='112' valign='top'><a href='{url}'>"
        f"<img src='{html.escape(img)}' width='100' style='border-radius:8px;display:block;'>"
        f"</a></td>"
        if img else ""
    )

    return f"""
<table cellpadding="0" cellspacing="0" style="{_CSS_CARD}width:100%;">
 <tr>
  {img_html}
  <td valign="top">
    <a href="{url}" style="font-size:15px;font-weight:600;color:#0b57d0;text-decoration:none;">{title}</a>
    <div style="margin:6px 0;font-size:16px;font-weight:600;">{price} {flag_html}</div>
    <div style="font-size:12px;color:#666;">score {hit.get('score')} &middot; matched: {why}</div>
  </td>
 </tr>
</table>"""


def compose(sections, borderline_sections=None, stats=None):
    """
    sections            : {alert_label: [hit, ...]}
    borderline_sections : {alert_label: [hit, ...]}  (lower confidence)
    stats               : {alert_label: {"median": int|None}}
    """
    borderline_sections = borderline_sections or {}
    stats = stats or {}

    total = sum(len(v) for v in sections.values())
    total_b = sum(len(v) for v in borderline_sections.values())

    if total:
        names = ", ".join(sections.keys())
        subject = f"Mercari: {total} new {'listing' if total == 1 else 'listings'} — {names}"
    else:
        subject = f"Mercari: {total_b} possible {'match' if total_b == 1 else 'matches'}"
    if len(subject) > 150:
        subject = subject[:147] + "..."

    html_parts = [
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:640px;margin:0 auto;\">"
    ]
    text_parts = []

    for label, hits in sections.items():
        median = (stats.get(label) or {}).get("median")
        html_parts.append(
            f"<h2 style='font-size:17px;margin:22px 0 10px;'>{html.escape(label)} "
            f"<span style='color:#888;font-weight:400;font-size:14px;'>({len(hits)})</span></h2>"
        )
        text_parts.append(f"== {label} ({len(hits)}) ==")
        for h in hits:
            html_parts.append(_card_html(h, median))
            text_parts.append(f"- {h.get('name')} | {_yen(h.get('price'))} | {item_url(h['id'])}")
        text_parts.append("")

    if borderline_sections:
        html_parts.append(
            "<hr style='border:none;border-top:1px solid #eee;margin:28px 0 8px;'>"
            "<h3 style='font-size:14px;color:#666;margin:0 0 4px;'>Possible matches</h3>"
            "<p style='font-size:12px;color:#888;margin:0 0 14px;'>These scored close to "
            "the threshold. If they look right, lower <code>min_score</code> for that "
            "alert; if they're noise, add an exclude term.</p>"
        )
        text_parts.append("== Possible matches (below threshold) ==")
        for label, hits in borderline_sections.items():
            html_parts.append(
                f"<h4 style='font-size:13px;color:#555;margin:14px 0 6px;'>{html.escape(label)}</h4>"
            )
            for h in hits:
                html_parts.append(_card_html(h))
                text_parts.append(
                    f"- [{h.get('score')}] {h.get('name')} | {_yen(h.get('price'))} "
                    f"| {item_url(h['id'])}"
                )
        text_parts.append("")

    html_parts.append("</div>")
    return subject, "\n".join(text_parts), "".join(html_parts)


def send(subject, text_body, html_body):
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addrs = [a.strip() for a in os.environ.get("ALERT_TO", user).split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=45) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, to_addrs, msg.as_string())
    return to_addrs

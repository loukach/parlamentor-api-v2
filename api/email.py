"""Email sending via Resend template API."""

import logging
import re

import httpx

from api.config import settings

logger = logging.getLogger(__name__)

ARTIFACT_LABELS = {
    "initiatives": "Iniciativas",
    "research": "Investigacao",
    "analysis": "Analise",
    "drafting": "Rascunho editorial",
}


def _md_to_html(text: str) -> str:
    """Convert basic markdown to HTML for email rendering."""
    html = text
    # Bold: **text** → <strong>text</strong>
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    # Italic: *text* → <em>text</em>
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    # Paragraphs: double newlines
    paragraphs = html.split("\n\n")
    html = "".join(f"<p>{p.strip()}</p>" for p in paragraphs if p.strip())
    return html


async def send_share_email(
    to_email: str, artifact_type: str, content: str, topic: str
) -> bool:
    """Send a branded share email via Resend template."""
    if not settings.resend_api_key or not settings.resend_share_template_id:
        logger.error("Resend not configured (missing API key or template ID)")
        return False

    label = ARTIFACT_LABELS.get(artifact_type, artifact_type)
    payload = {
        "from": "Parlamentor <notificacoes@notifications.parla-app.eu>",
        "to": [to_email],
        "subject": f"Parlamentor: {topic} \u2014 {label}",
        "template": {
            "id": settings.resend_share_template_id,
            "variables": {
                "topic": topic[:200],
                "artifact_label": label,
                "content": _md_to_html(content[:2_000]),
                "app_url": settings.frontend_url,
            },
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
            if resp.status_code == 200:
                result = resp.json()
                logger.info("Share email sent to %s: %s", to_email, result.get("id"))
                return True
            logger.error("Resend HTTP %s: %s", resp.status_code, resp.text)
            return False
        except httpx.HTTPError as e:
            logger.error("Resend request error: %s", e)
            return False
